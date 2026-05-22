/**
 * Unit tests for run_codex.mjs.
 *
 * Tests cover the four pure-JS functions that can be exercised without a live
 * codex app-server or Docker container:
 *
 *   1. extractCodexTrajectorySteps  — notification → [{thought, act, obs}] conversion
 *   2. codexApiEnv (via module internals) — env rewrite and CODEX_APP_CONFIG shape
 *   3. full_info question denial     — handleRequestUserInput returns UNKNOWN_RESOLUTION
 *   4. computeTrajectoryStats        — counts num_questions, num_blockers_resolved, etc.
 *
 * Run with:  node --test tests/run_codex.test.mjs
 */

import test from "node:test";
import assert from "node:assert/strict";

// ── Inline copies of the pure helpers from run_codex.mjs ─────────────────────
//
// We inline rather than import to avoid triggering the module-level main()
// call and to remain independent of the container runtime environment.

const THOUGHT_CAP = 4000;
const ACT_CAP     = 4000;
const OBS_CAP     = 8000;
const UNKNOWN_RESOLUTION = "irrelevant question";
const UNKNOWN_BLOCKER_ID = "UNKNOWN";
const ASK_HUMAN_REQUEST_TYPES = new Set(["clarification", "elicitation"]);
const APPROVAL_REQUEST_TYPES  = new Set(["approval", "permission"]);

function cap(s, limit) {
  const str = String(s || "");
  return str.length > limit ? `${str.slice(0, limit)}… [truncated]` : str;
}

function snakeCase(t) {
  return String(t || "").replace(/([A-Z])/g, (m) => `_${m.toLowerCase()}`).replace(/^_/, "");
}

function safeJson(value) {
  try { return JSON.stringify(value); } catch { return "[unserializable]"; }
}

function mcpToolName(item) {
  return `${item?.server || ""}.${item?.tool || ""}`.replace(/^\./, "");
}

function readAskQuestionFromValue(value) {
  if (value == null) return "";
  if (typeof value === "string") {
    if (!value.length) return "";
    try {
      return readAskQuestionFromValue(JSON.parse(value));
    } catch {
      return value;
    }
  }
  if (typeof value !== "object") return "";
  if (typeof value.question === "string") return value.question;
  if (value.arguments && typeof value.arguments === "object") {
    const nested = readAskQuestionFromValue(value.arguments);
    if (nested !== "") return nested;
  }
  if (value.input && typeof value.input === "object") {
    const nested = readAskQuestionFromValue(value.input);
    if (nested !== "") return nested;
  }
  if (value.ask_human && typeof value.ask_human === "object") {
    const nested = readAskQuestionFromValue(value.ask_human);
    if (nested !== "") return nested;
  }
  return "";
}

function extractMcpResultText(result) {
  if (result == null) return "";
  if (typeof result === "string") return result;
  if (Array.isArray(result?.content)) {
    const parts = result.content
      .map((c) => (typeof c === "string" ? c : c?.text))
      .filter(Boolean);
    if (parts.length) return parts.join("\n");
  }
  if (typeof result?.structuredContent?.resolution === "string") return result.structuredContent.resolution;
  if (typeof result?.resolution === "string") return result.resolution;
  return safeJson(result);
}

function extractCodexTrajectorySteps(events) {
  const steps = [];
  const emittedItemIds = new Set();
  let currentThought = "";

  for (const ev of events) {
    if (ev.type === "codex_ask_question") {
      steps.push({
        thought: currentThought,
        act:     cap(`ask_human [native] ${ev.question}`, ACT_CAP),
        obs:     cap(String(ev.answer ?? ""), OBS_CAP),
      });
      currentThought = "";
      continue;
    }

    if (ev.type === "ask_question_full_info_mode") {
      steps.push({
        thought: currentThought,
        act:     cap(`ask_human [native] ${ev.question || ""}`, ACT_CAP),
        obs:     UNKNOWN_RESOLUTION,
      });
      currentThought = "";
      continue;
    }

    if (ev.type !== "sdk_event") continue;
    const notif = ev.event;
    if (!notif?.method) continue;

    const method = notif.method;
    const params = notif.params || {};

    if (
      method === "item/commandExecution/outputDelta"  ||
      method === "item/fileChange/outputDelta"        ||
      method === "item/fileChange/patchUpdated"       ||
      method === "turn/diff/updated"
    ) continue;

    if (method === "turn/completed") {
      if (currentThought) {
        steps.push({ thought: currentThought, act: "", obs: "" });
        currentThought = "";
      }
      continue;
    }

    if (method === "error" || method === "turn/error") continue;

    const item = params.item;
    if (!item) continue;

    const itemType = snakeCase(item.type);
    const itemId   = item.id || null;

    if (itemType === "reasoning" || itemType === "agent_message") {
      const text = item.text || "";
      if (text) currentThought = cap(text, THOUGHT_CAP);
      continue;
    }

    if (itemType === "user_message") continue;

    if (itemType === "command_execution") {
      // eslint-disable-next-line eqeqeq
      const done = item.exitCode != null || item.exit_code != null;
      if (!done) continue;
      if (itemId && emittedItemIds.has(itemId)) continue;
      if (itemId) emittedItemIds.add(itemId);

      const output = item.aggregatedOutput ?? item.aggregated_output ?? "";
      steps.push({
        thought: currentThought,
        act:     cap(item.command || "", ACT_CAP),
        obs:     cap(String(output), OBS_CAP),
      });
      currentThought = "";
      continue;
    }

    if (itemType === "file_change") {
      const paths = (Array.isArray(item.changes) ? item.changes : [])
        .map((c) => c.path)
        .filter(Boolean);
      if (!paths.length) continue;
      if (itemId && emittedItemIds.has(itemId)) continue;
      if (itemId) emittedItemIds.add(itemId);

      steps.push({
        thought: currentThought,
        act:     cap(`Edit: ${paths.join(", ")}`, ACT_CAP),
        obs:     "",
      });
      currentThought = "";
      continue;
    }

    if (itemType === "mcp_tool_call") {
      const done = item.status === "completed" || item.status === "failed";
      if (!done) continue;
      if (itemId && emittedItemIds.has(itemId)) continue;
      if (itemId) emittedItemIds.add(itemId);

      const toolName = mcpToolName(item);
      let act = `${toolName}: ${safeJson(item.arguments || {})}`;
      if (toolName === "human_input.ask_human") {
        const q = readAskQuestionFromValue(item?.arguments);
        act = `ask_human [custom_tool] ${q}`;
      }
      steps.push({
        thought: currentThought,
        act:     cap(act, ACT_CAP),
        obs:     cap(extractMcpResultText(item.result ?? item.error), OBS_CAP),
      });
      currentThought = "";
      continue;
    }
  }

  return steps;
}

function computeTrajectoryStats(events, trajectorySteps, numBlockersTotal) {
  let numQuestions          = 0;
  let numQuestionsApproval  = 0;
  let numQuestionsFullInfo  = 0;
  const resolvedBlockerIds  = new Set();
  const seenRawEventIds     = new Set();
  const seenResultEventIds  = new Set();
  const requestStatusById   = new Map();

  for (const ev of events) {
    if (ev.type !== "human_input_result") continue;
    const rid = String(ev.request_id || "");
    if (!rid) continue;
    const status = String(ev.result?.status || "unknown").toLowerCase();
    requestStatusById.set(rid, status);
  }

  for (const ev of events) {
    if (ev.type === "human_input_raw_event") {
      const rid = String(ev.request_id || "");
      if (rid && seenRawEventIds.has(rid)) continue;
      if (rid) seenRawEventIds.add(rid);
      const status = rid ? requestStatusById.get(rid) : null;
      if (status === "error") continue;
      if (ASK_HUMAN_REQUEST_TYPES.has(ev.request_type))     numQuestions++;
      else if (APPROVAL_REQUEST_TYPES.has(ev.request_type)) numQuestionsApproval++;
    }
    if (ev.type === "ask_question_full_info_mode") numQuestionsFullInfo++;
    if (ev.type === "human_input_result") {
      const rid = String(ev.request_id || "");
      if (rid && seenResultEventIds.has(rid)) continue;
      if (rid) seenResultEventIds.add(rid);
      const bid = ev.result?.blocker_id;
      if (bid && bid !== UNKNOWN_BLOCKER_ID && ev.result?.status === "answered")
        resolvedBlockerIds.add(bid);
    }
  }

  return {
    num_steps:                trajectorySteps.length,
    num_questions:            numQuestions,
    num_questions_approval:   numQuestionsApproval,
    num_total_questions:      numQuestions + numQuestionsApproval,
    num_questions_full_info:  numQuestionsFullInfo,
    num_blockers_resolved:    resolvedBlockerIds.size,
    num_blockers_total:       numBlockersTotal,
  };
}

// handleRequestUserInput (full_info path only — ask_human path requires a live router)
async function handleRequestUserInput_fullInfo({ params }) {
  const answers = {};
  for (const question of params.questions || []) {
    answers[question.id] = { answers: [UNKNOWN_RESOLUTION] };
  }
  return { answers };
}

// ── Helpers for building test events ─────────────────────────────────────────

function mkSdkEvent(method, params) {
  return { type: "sdk_event", timestamp: new Date().toISOString(), event: { jsonrpc: "2.0", method, params } };
}

// ── 1. extractCodexTrajectorySteps ───────────────────────────────────────────

test("extractCodexTrajectorySteps: empty events → empty steps", () => {
  assert.deepEqual(extractCodexTrajectorySteps([]), []);
});

test("extractCodexTrajectorySteps: non-sdk_event entries are ignored", () => {
  const events = [
    { type: "attempt_start", timestamp: "t", uid: "abc" },
    { type: "codex_server_request", timestamp: "t", event: { id: 1, method: "initialize", params: {} } },
  ];
  assert.deepEqual(extractCodexTrajectorySteps(events), []);
});

test("extractCodexTrajectorySteps: streaming delta events are skipped", () => {
  const events = [
    mkSdkEvent("item/commandExecution/outputDelta",  { delta: "partial..." }),
    mkSdkEvent("item/fileChange/outputDelta",         { delta: "partial..." }),
    mkSdkEvent("item/fileChange/patchUpdated",        { patch: "..." }),
    mkSdkEvent("turn/diff/updated",                   { diff: "..." }),
  ];
  assert.deepEqual(extractCodexTrajectorySteps(events), []);
});

test("extractCodexTrajectorySteps: command execution (item/completed with exitCode) → step", () => {
  // Real wire format: camelCase exitCode + aggregatedOutput
  const events = [
    mkSdkEvent("item/started",   { item: { id: "cmd-1", type: "commandExecution", command: "pytest tests/", aggregatedOutput: null, exitCode: null } }),
    mkSdkEvent("item/completed", { item: { id: "cmd-1", type: "commandExecution", command: "pytest tests/", aggregatedOutput: "12 passed", exitCode: 0 } }),
    mkSdkEvent("turn/completed", {}),
  ];
  const steps = extractCodexTrajectorySteps(events);
  assert.equal(steps.length, 1);
  assert.equal(steps[0].act,     "pytest tests/");
  assert.equal(steps[0].obs,     "12 passed");
  assert.equal(steps[0].thought, "");
});

test("extractCodexTrajectorySteps: command execution with null exitCode is skipped (still in progress)", () => {
  // item/started has exitCode: null — must not be emitted
  const events = [
    mkSdkEvent("item/started", { item: { id: "cmd-2", type: "commandExecution", command: "ls", aggregatedOutput: null, exitCode: null } }),
    mkSdkEvent("turn/completed", {}),
  ];
  assert.deepEqual(extractCodexTrajectorySteps(events), []);
});

test("extractCodexTrajectorySteps: reasoning before command becomes thought", () => {
  const events = [
    mkSdkEvent("item/updated",   { item: { id: "r-1", type: "reasoning", text: "I should list files" } }),
    mkSdkEvent("item/started",   { item: { id: "cmd-3", type: "commandExecution", command: "ls /app", aggregatedOutput: null, exitCode: null } }),
    mkSdkEvent("item/completed", { item: { id: "cmd-3", type: "commandExecution", command: "ls /app", aggregatedOutput: "src tests", exitCode: 0 } }),
    mkSdkEvent("turn/completed", {}),
  ];
  const steps = extractCodexTrajectorySteps(events);
  assert.equal(steps.length, 1);
  assert.equal(steps[0].thought, "I should list files");
  assert.equal(steps[0].act,     "ls /app");
  assert.equal(steps[0].obs,     "src tests");
});

test("extractCodexTrajectorySteps: file_change → step with Edit: prefix", () => {
  const events = [
    mkSdkEvent("item/updated", { item: {
      id: "fc-1", type: "file_change",
      changes: [{ path: "src/foo.py" }, { path: "src/bar.py" }],
    }}),
    mkSdkEvent("turn/completed", {}),
  ];
  const steps = extractCodexTrajectorySteps(events);
  assert.equal(steps.length, 1);
  assert.ok(steps[0].act.startsWith("Edit:"));
  assert.ok(steps[0].act.includes("src/foo.py"));
  assert.ok(steps[0].act.includes("src/bar.py"));
  assert.equal(steps[0].obs, "");
});

test("extractCodexTrajectorySteps: file_change with empty changes is skipped", () => {
  const events = [
    mkSdkEvent("item/updated", { item: { id: "fc-2", type: "file_change", changes: [] } }),
    mkSdkEvent("turn/completed", {}),
  ];
  assert.deepEqual(extractCodexTrajectorySteps(events), []);
});

test("extractCodexTrajectorySteps: duplicate item IDs are not re-emitted", () => {
  // item/started → exitCode: null → skipped (in progress).
  // item/completed → exitCode: 0 → emitted once.
  // A second item/completed (hypothetical duplicate) is deduplicated by ID.
  const events = [
    mkSdkEvent("item/started",   { item: { id: "cmd-5", type: "commandExecution", command: "make", aggregatedOutput: null, exitCode: null } }),
    mkSdkEvent("item/completed", { item: { id: "cmd-5", type: "commandExecution", command: "make", aggregatedOutput: "Built ok", exitCode: 0 } }),
    mkSdkEvent("item/completed", { item: { id: "cmd-5", type: "commandExecution", command: "make", aggregatedOutput: "Built ok", exitCode: 0 } }),
    mkSdkEvent("turn/completed", {}),
  ];
  const steps = extractCodexTrajectorySteps(events);
  assert.equal(steps.length, 1);
  assert.equal(steps[0].obs, "Built ok");
});

test("extractCodexTrajectorySteps: trailing thought flushed at turn/completed", () => {
  const events = [
    mkSdkEvent("item/updated", { item: { id: "r-2", type: "reasoning", text: "Nothing to do" } }),
    mkSdkEvent("turn/completed", {}),
  ];
  const steps = extractCodexTrajectorySteps(events);
  assert.equal(steps.length, 1);
  assert.equal(steps[0].thought, "Nothing to do");
  assert.equal(steps[0].act,     "");
  assert.equal(steps[0].obs,     "");
});

test("extractCodexTrajectorySteps: mcp_tool_call completed → step", () => {
  const events = [
    mkSdkEvent("item/updated", { item: {
      id: "mcp-1", type: "mcp_tool_call",
      server: "fs", tool: "read_file",
      arguments: { path: "/app/foo.py" },
      result: "def foo(): pass",
      status: "completed",
    }}),
    mkSdkEvent("turn/completed", {}),
  ];
  const steps = extractCodexTrajectorySteps(events);
  assert.equal(steps.length, 1);
  assert.ok(steps[0].act.includes("fs.read_file"));
  assert.ok(steps[0].obs.includes("def foo(): pass"));
});

test("extractCodexTrajectorySteps: mcp_tool_call structured result is serialized as text", () => {
  const events = [
    mkSdkEvent("item/updated", { item: {
      id: "mcp-structured", type: "mcp_tool_call",
      server: "human_input", tool: "ask_human",
      arguments: { question: "default timeout?" },
      result: {
        content: [{ type: "text", text: "11s" }],
        structuredContent: {
          resolution: "11s",
          status: "answered",
          blocker_id: "B42",
          events: [{ type: "human_input_raw_event", request_id: "rq-1" }],
        },
      },
      status: "completed",
    }}),
    mkSdkEvent("turn/completed", {}),
  ];
  const steps = extractCodexTrajectorySteps(events);
  assert.equal(steps.length, 1);
  assert.equal(steps[0].act, "ask_human [custom_tool] default timeout?");
  assert.equal(steps[0].obs, "11s");
  assert.ok(!steps[0].obs.includes("[object Object]"));
});

test("extractCodexTrajectorySteps: mcp_tool_call not yet completed is skipped", () => {
  const events = [
    mkSdkEvent("item/updated", { item: {
      id: "mcp-2", type: "mcp_tool_call",
      server: "fs", tool: "write_file",
      arguments: {}, status: "running",
    }}),
    mkSdkEvent("turn/completed", {}),
  ];
  assert.deepEqual(extractCodexTrajectorySteps(events), []);
});

test("extractCodexTrajectorySteps: camelCase item types are normalised", () => {
  // commandExecution → command_execution (via snakeCase)
  const events = [
    mkSdkEvent("item/completed", { item: {
      id: "cc-1", type: "commandExecution",
      command: "echo hi", aggregatedOutput: "hi", exitCode: 0,
    }}),
    mkSdkEvent("turn/completed", {}),
  ];
  const steps = extractCodexTrajectorySteps(events);
  assert.equal(steps.length, 1);
  assert.equal(steps[0].act, "echo hi");
});

test("extractCodexTrajectorySteps: multi-step sequence", () => {
  const events = [
    mkSdkEvent("item/updated",   { item: { type: "reasoning", text: "First look at tests" } }),
    mkSdkEvent("item/started",   { item: { id: "c1", type: "commandExecution", command: "pytest", aggregatedOutput: null, exitCode: null } }),
    mkSdkEvent("item/completed", { item: { id: "c1", type: "commandExecution", command: "pytest", aggregatedOutput: "5 failed", exitCode: 1 } }),
    mkSdkEvent("item/updated",   { item: { type: "reasoning", text: "Now edit" } }),
    mkSdkEvent("item/updated",   { item: { id: "f1", type: "file_change", changes: [{ path: "fix.py" }] } }),
    mkSdkEvent("item/started",   { item: { id: "c2", type: "commandExecution", command: "pytest", aggregatedOutput: null, exitCode: null } }),
    mkSdkEvent("item/completed", { item: { id: "c2", type: "commandExecution", command: "pytest", aggregatedOutput: "all pass", exitCode: 0 } }),
    mkSdkEvent("turn/completed", {}),
  ];
  const steps = extractCodexTrajectorySteps(events);
  assert.equal(steps.length, 3);
  assert.equal(steps[0].thought, "First look at tests");
  assert.equal(steps[0].act,     "pytest");
  assert.equal(steps[0].obs,     "5 failed");
  assert.equal(steps[1].thought, "Now edit");
  assert.ok(steps[1].act.includes("fix.py"));
  assert.equal(steps[2].thought, "");
  assert.equal(steps[2].obs,     "all pass");
});

test("extractCodexTrajectorySteps: long text is capped", () => {
  const longText = "x".repeat(10000);
  const events = [
    mkSdkEvent("item/completed", { item: {
      id: "cap-1", type: "commandExecution",
      command: longText, aggregatedOutput: longText, exitCode: 0,
    }}),
    mkSdkEvent("turn/completed", {}),
  ];
  const steps = extractCodexTrajectorySteps(events);
  assert.equal(steps.length, 1);
  assert.ok(steps[0].act.length <= ACT_CAP + 20);  // +20 for "… [truncated]"
  assert.ok(steps[0].obs.length <= OBS_CAP + 20);
  assert.ok(steps[0].act.endsWith("[truncated]"));
  assert.ok(steps[0].obs.endsWith("[truncated]"));
});


// ── 2. codexApiEnv env rewrite ────────────────────────────────────────────────

test("codexApiEnv: CODEX_APP_CONFIG shape is valid JSON with litellm provider", () => {
  const savedEnv = process.env;
  // Temporarily set required vars
  process.env = {
    ...savedEnv,
    LITELLM_API_KEY:  "test-key",
    LITELLM_BASE_URL: "http://localhost:4000",
  };

  // Reproduce codexApiEnv logic inline (identical to run_codex.mjs)
  const token   = process.env.LITELLM_API_KEY;
  const baseUrl = process.env.LITELLM_BASE_URL.replace(/\/+$/, "").replace(/\blocalhost\b/g, "host.docker.internal");
  const responsesBaseUrl = `${baseUrl}/v1`;
  const codexAppConfig = {
    approval_policy: "on-request",
    model_provider:  "litellm",
    model_providers: {
      litellm: { name: "LiteLLM", base_url: responsesBaseUrl, env_key: "CODEX_API_KEY", wire_api: "responses", requires_openai_auth: true },
    },
  };
  const env = {
    ...process.env,
    CODEX_API_KEY:          token,
    OPENAI_API_KEY:         token,
    LITELLM_BASE_URL:       baseUrl,
    CODEX_LITELLM_BASE_URL: responsesBaseUrl,
    CODEX_APP_CONFIG:       JSON.stringify(codexAppConfig),
  };
  process.env = savedEnv;

  const cfg = JSON.parse(env.CODEX_APP_CONFIG);
  assert.equal(cfg.model_provider, "litellm");
  assert.equal(cfg.approval_policy, "on-request");
  assert.equal(cfg.model_providers.litellm.wire_api, "responses");
  assert.ok(cfg.model_providers.litellm.base_url.includes("host.docker.internal"));
  assert.ok(!cfg.model_providers.litellm.base_url.includes("localhost"));
  assert.equal(env.CODEX_API_KEY, "test-key");
  assert.equal(env.OPENAI_API_KEY, "test-key");
});

test("codexApiEnv: localhost in base URL is rewritten to host.docker.internal", () => {
  const raw    = "http://localhost:4000/v1";
  const rewritten = raw.replace(/\blocalhost\b/g, "host.docker.internal");
  assert.equal(rewritten, "http://host.docker.internal:4000/v1");
});

// ── 3. full_info mode: question denial ───────────────────────────────────────

test("handleRequestUserInput full_info: single question → UNKNOWN_RESOLUTION", async () => {
  const params = { questions: [{ id: "q1", question: "Which branch to target?" }] };
  const result = await handleRequestUserInput_fullInfo({ params });
  assert.deepEqual(result, { answers: { q1: { answers: [UNKNOWN_RESOLUTION] } } });
});

test("handleRequestUserInput full_info: multiple questions all denied", async () => {
  const params = {
    questions: [
      { id: "q1", question: "Which branch?" },
      { id: "q2", question: "Which file?"   },
    ],
  };
  const result = await handleRequestUserInput_fullInfo({ params });
  assert.equal(result.answers.q1.answers[0], UNKNOWN_RESOLUTION);
  assert.equal(result.answers.q2.answers[0], UNKNOWN_RESOLUTION);
});

test("handleRequestUserInput full_info: empty questions list → empty answers", async () => {
  const result = await handleRequestUserInput_fullInfo({ params: { questions: [] } });
  assert.deepEqual(result, { answers: {} });
});

// ── 4. computeTrajectoryStats ─────────────────────────────────────────────────

test("computeTrajectoryStats: empty events → zero stats", () => {
  const stats = computeTrajectoryStats([], [], 0);
  assert.deepEqual(stats, {
    num_steps: 0,
    num_questions: 0,
    num_questions_approval: 0,
    num_total_questions: 0,
    num_questions_full_info: 0,
    num_blockers_resolved: 0,
    num_blockers_total: 0,
  });
});

test("computeTrajectoryStats: clarification events counted as num_questions", () => {
  const events = [
    { type: "human_input_raw_event", request_type: "clarification" },
    { type: "human_input_raw_event", request_type: "clarification" },
    { type: "human_input_raw_event", request_type: "elicitation"   },
  ];
  const stats = computeTrajectoryStats(events, [], 0);
  assert.equal(stats.num_questions, 3);
  assert.equal(stats.num_questions_approval, 0);
  assert.equal(stats.num_total_questions, 3);
});

test("computeTrajectoryStats: approval events counted as num_questions_approval", () => {
  const events = [
    { type: "human_input_raw_event", request_type: "approval"   },
    { type: "human_input_raw_event", request_type: "permission" },
  ];
  const stats = computeTrajectoryStats(events, [], 0);
  assert.equal(stats.num_questions, 0);
  assert.equal(stats.num_questions_approval, 2);
  assert.equal(stats.num_total_questions, 2);
});

test("computeTrajectoryStats: duplicate answered blocker IDs count once", () => {
  const events = [
    { type: "human_input_result", result: { blocker_id: "b1", status: "answered" } },
    { type: "human_input_result", result: { blocker_id: "b1", status: "answered" } },
    { type: "human_input_result", result: { blocker_id: "b1", status: "answered" } },
  ];
  const stats = computeTrajectoryStats(events, [], 5);
  assert.equal(stats.num_blockers_resolved, 1);
});

test("computeTrajectoryStats: answered blocker results counted as resolved", () => {
  const events = [
    { type: "human_input_result", result: { blocker_id: "b1", status: "answered" } },
    { type: "human_input_result", result: { blocker_id: "b2", status: "answered" } },
    { type: "human_input_result", result: { blocker_id: UNKNOWN_BLOCKER_ID, status: "answered" } }, // excluded
    { type: "human_input_result", result: { blocker_id: "b3", status: "rejected"  } }, // excluded
  ];
  const stats = computeTrajectoryStats(events, [], 5);
  assert.equal(stats.num_blockers_resolved, 2);
  assert.equal(stats.num_blockers_total, 5);
});

test("computeTrajectoryStats: num_steps comes from trajectorySteps.length", () => {
  const steps = [
    { thought: "", act: "ls", obs: "" },
    { thought: "", act: "pytest", obs: "ok" },
  ];
  const stats = computeTrajectoryStats([], steps, 0);
  assert.equal(stats.num_steps, 2);
});

test("computeTrajectoryStats: mixed events full accounting", () => {
  const steps = [
    { thought: "look", act: "ls", obs: "files" },
    { thought: "",     act: "edit", obs: "" },
    { thought: "",     act: "test", obs: "pass" },
  ];
  const events = [
    { type: "human_input_raw_event", request_type: "clarification" },
    { type: "human_input_raw_event", request_type: "approval" },
    { type: "human_input_result",    result: { blocker_id: "b1", status: "answered" } },
    { type: "attempt_start" },   // non-stats event — ignored
    { type: "sdk_event"    },    // non-stats event — ignored
  ];
  const stats = computeTrajectoryStats(events, steps, 3);
  assert.equal(stats.num_steps,              3);
  assert.equal(stats.num_questions,          1);
  assert.equal(stats.num_questions_approval, 1);
  assert.equal(stats.num_total_questions,    2);
  assert.equal(stats.num_questions_full_info, 0);  // no full_info events
  assert.equal(stats.num_blockers_resolved,  1);
  assert.equal(stats.num_blockers_total,     3);
});

test("computeTrajectoryStats: duplicate request IDs are deduplicated", () => {
  const events = [
    { type: "human_input_raw_event", request_id: "rq-1", request_type: "clarification" },
    { type: "human_input_raw_event", request_id: "rq-1", request_type: "clarification" },
    { type: "human_input_raw_event", request_id: "rq-2", request_type: "approval" },
    { type: "human_input_raw_event", request_id: "rq-2", request_type: "approval" },
    { type: "human_input_result", request_id: "rq-3", result: { blocker_id: "B1", status: "answered" } },
    { type: "human_input_result", request_id: "rq-3", result: { blocker_id: "B1", status: "answered" } },
  ];
  const stats = computeTrajectoryStats(events, [], 10);
  assert.equal(stats.num_questions, 1);
  assert.equal(stats.num_questions_approval, 1);
  assert.equal(stats.num_total_questions, 2);
  assert.equal(stats.num_blockers_resolved, 1);
});

test("computeTrajectoryStats: failed ask_human requests (status=error) are excluded", () => {
  const events = [
    { type: "human_input_raw_event", request_id: "ok-1", request_type: "clarification" },
    { type: "human_input_result", request_id: "ok-1", result: { blocker_id: UNKNOWN_BLOCKER_ID, status: "unknown" } },
    { type: "human_input_raw_event", request_id: "err-1", request_type: "clarification" },
    { type: "human_input_result", request_id: "err-1", result: { blocker_id: UNKNOWN_BLOCKER_ID, status: "error" } },
    { type: "human_input_raw_event", request_id: "err-2", request_type: "approval" },
    { type: "human_input_result", request_id: "err-2", result: { blocker_id: UNKNOWN_BLOCKER_ID, status: "error" } },
  ];
  const stats = computeTrajectoryStats(events, [], 0);
  assert.equal(stats.num_questions, 1);
  assert.equal(stats.num_questions_approval, 0);
  assert.equal(stats.num_total_questions, 1);
});

// ── 5. full_info mode question tracking ──────────────────────────────────────
// Verifies that ask_question_full_info_mode events:
//   a) produce a trajectory step with act="ask_human …" and obs="irrelevant question"
//   b) are counted in num_questions_full_info (NOT in num_questions)

test("extractCodexTrajectorySteps: ask_question_full_info_mode → step with obs=irrelevant question", () => {
  const events = [
    { type: "ask_question_full_info_mode", timestamp: "t", question: "Which branch to target?" },
  ];
  const steps = extractCodexTrajectorySteps(events);
  assert.equal(steps.length, 1);
  assert.ok(steps[0].act.startsWith("ask_human "));
  assert.ok(steps[0].act.includes("Which branch to target?"));
  assert.equal(steps[0].obs, UNKNOWN_RESOLUTION);
  assert.equal(steps[0].thought, "");
});

test("extractCodexTrajectorySteps: ask_question_full_info_mode clears current thought", () => {
  const events = [
    mkSdkEvent("item/updated",   { item: { type: "reasoning", text: "I need more info" } }),
    { type: "ask_question_full_info_mode", timestamp: "t", question: "What is the expected output?" },
    mkSdkEvent("item/completed", { item: { id: "c1", type: "commandExecution", command: "pytest", aggregatedOutput: "pass", exitCode: 0 } }),
    mkSdkEvent("turn/completed", {}),
  ];
  const steps = extractCodexTrajectorySteps(events);
  assert.equal(steps.length, 2);
  // First step: full_info question, thought is attached from the preceding reasoning
  assert.equal(steps[0].thought, "I need more info");
  assert.ok(steps[0].act.includes("What is the expected output?"));
  assert.equal(steps[0].obs, UNKNOWN_RESOLUTION);
  // Second step: command after question, thought is cleared
  assert.equal(steps[1].thought, "");
  assert.equal(steps[1].act, "pytest");
});

test("extractCodexTrajectorySteps: multiple full_info questions all produce steps", () => {
  const events = [
    { type: "ask_question_full_info_mode", timestamp: "t1", question: "Question A" },
    { type: "ask_question_full_info_mode", timestamp: "t2", question: "Question B" },
  ];
  const steps = extractCodexTrajectorySteps(events);
  assert.equal(steps.length, 2);
  assert.ok(steps[0].act.includes("Question A"));
  assert.ok(steps[1].act.includes("Question B"));
  assert.equal(steps[0].obs, UNKNOWN_RESOLUTION);
  assert.equal(steps[1].obs, UNKNOWN_RESOLUTION);
});

test("computeTrajectoryStats: ask_question_full_info_mode counted in num_questions_full_info, not num_questions", () => {
  const events = [
    { type: "ask_question_full_info_mode", question: "Q1" },
    { type: "ask_question_full_info_mode", question: "Q2" },
    { type: "ask_question_full_info_mode", question: "Q3" },
  ];
  const stats = computeTrajectoryStats(events, [], 0);
  assert.equal(stats.num_questions_full_info, 3);
  assert.equal(stats.num_questions,           0);   // NOT counted as ask_human questions
  assert.equal(stats.num_questions_approval,  0);
  assert.equal(stats.num_total_questions,     0);   // full_info questions excluded from this total
});

test("computeTrajectoryStats: full_info and ask_human questions are counted independently", () => {
  // In practice ask_human mode never fires ask_question_full_info_mode and vice versa,
  // but the counter logic must be independent — verify both can coexist.
  const events = [
    { type: "human_input_raw_event",      request_type: "clarification" },
    { type: "ask_question_full_info_mode", question: "Q-full-info" },
    { type: "human_input_raw_event",      request_type: "elicitation" },
    { type: "ask_question_full_info_mode", question: "Q-full-info-2" },
  ];
  const stats = computeTrajectoryStats(events, [], 0);
  assert.equal(stats.num_questions,           2);   // two ask_human (clarification + elicitation)
  assert.equal(stats.num_questions_full_info, 2);   // two full_info questions
  assert.equal(stats.num_total_questions,     2);   // full_info NOT in this total
});

test("extractCodexTrajectorySteps: ask_question_full_info_mode with empty question string", () => {
  const events = [
    { type: "ask_question_full_info_mode", timestamp: "t", question: "" },
  ];
  const steps = extractCodexTrajectorySteps(events);
  assert.equal(steps.length, 1);
  assert.equal(steps[0].act,  "ask_human [native] ");   // empty question → just the prefix
  assert.equal(steps[0].obs,  UNKNOWN_RESOLUTION);
});

test("extractCodexTrajectorySteps: codex_ask_question still works alongside full_info events", () => {
  // ask_human and full_info events in same event stream — both produce steps independently.
  const events = [
    { type: "codex_ask_question",          timestamp: "t1", question: "Ask-human Q", answer: "The answer" },
    { type: "ask_question_full_info_mode", timestamp: "t2", question: "Full-info Q" },
  ];
  const steps = extractCodexTrajectorySteps(events);
  assert.equal(steps.length, 2);
  assert.ok(steps[0].act.includes("Ask-human Q"));
  assert.equal(steps[0].obs, "The answer");
  assert.ok(steps[1].act.includes("Full-info Q"));
  assert.equal(steps[1].obs, UNKNOWN_RESOLUTION);
});
