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

function extractCodexTrajectorySteps(events) {
  const steps = [];
  const emittedItemIds = new Set();
  let currentThought = "";

  for (const ev of events) {
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

      const toolName = `${item.server || ""}.${item.tool || ""}`.replace(/^\./, "");
      steps.push({
        thought: currentThought,
        act:     cap(`${toolName}: ${JSON.stringify(item.arguments || {})}`, ACT_CAP),
        obs:     cap(String(item.result || item.error || ""), OBS_CAP),
      });
      currentThought = "";
      continue;
    }
  }

  return steps;
}

function computeTrajectoryStats(events, trajectorySteps, numBlockersTotal) {
  let numQuestions         = 0;
  let numQuestionsApproval = 0;
  let numBlockersResolved  = 0;

  for (const ev of events) {
    if (ev.type === "human_input_raw_event") {
      if (ASK_HUMAN_REQUEST_TYPES.has(ev.request_type))       numQuestions++;
      else if (APPROVAL_REQUEST_TYPES.has(ev.request_type)) numQuestionsApproval++;
    }
    if (ev.type === "human_input_result") {
      const bid = ev.result?.blocker_id;
      if (bid && bid !== UNKNOWN_BLOCKER_ID && ev.result?.status === "answered")
        numBlockersResolved++;
    }
  }

  return {
    num_steps:              trajectorySteps.length,
    num_questions:          numQuestions,
    num_questions_approval: numQuestionsApproval,
    num_total_questions:    numQuestions + numQuestionsApproval,
    num_blockers_resolved:  numBlockersResolved,
    num_blockers_total:     numBlockersTotal,
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
  assert.equal(stats.num_blockers_resolved,  1);
  assert.equal(stats.num_blockers_total,     3);
});
