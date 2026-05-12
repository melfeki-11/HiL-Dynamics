/**
 * ask_human sidecar for run_adk.py
 *
 * Wraps the trust_horizon human input router (human_input.mjs) in a minimal HTTP
 * server so the Python ADK harness can call it for question routing without
 * reimplementing the LLM judge logic in Python.
 *
 * Startup protocol (read by run_adk.py):
 *   The server writes "SIDECAR_PORT=<n>\n" to stdout immediately after binding.
 *   run_adk.py reads that line from the subprocess's stdout to learn the port.
 *
 * Endpoints:
 *   POST /ask    — route a question through the human input router
 *     Request:  { question, options?, context?, request_type?, native_event_type?, raw_event? }
 *     Response: { resolution, selected_labels, blocker_id, status, events }
 *               `events` is an array of human_input_* / ask_question_full_info_mode events
 *               that run_adk.py appends to its all_events list for stats/trajectory.
 *
 *   GET  /health — liveness probe; returns 200 { ok: true }
 *
 * Mode handling:
 *   ask_human mode:  route through createHumanInputRouter (LLM judge)
 *   full_info mode:  return "irrelevant question" immediately + emit
 *                    ask_question_full_info_mode event (for num_questions_full_info tracking)
 */

import http    from "node:http";
import path    from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

import {
  createHumanInputRouter,
  UNKNOWN_RESOLUTION,
  UNKNOWN_BLOCKER_ID,
} from "../shared/human_input.mjs";
import { ASK_HUMAN_BASE_URL, ASK_HUMAN_MODEL } from "./constants.mjs";

// ── Environment ───────────────────────────────────────────────────────────────
const MODE     = process.env.MODE     || "ask_human";
const TASK_DIR = process.env.TASK_DIR || "/task";
const TASK_UID = process.env.TASK_UID || "unknown";  // set by run_adk.py before Popen

// ── Human input router ────────────────────────────────────────────────────────
// Only created in ask_human mode.  The trajectoryFile callback funnels events
// into the per-request capture array via the mutable _eventSink reference.
//
// Concurrency note: _eventSink is a module-level mutable reference swapped
// per-request.  This is safe because:
//   (a) Node.js is single-threaded — concurrent HTTP handlers interleave only
//       at `await` boundaries;
//   (b) the ADK agent makes tool calls sequentially — only one /ask request
//       can be in-flight at a time per sidecar instance.
// If that assumption ever changes, replace with a request-local capture.
let _eventSink = null;
let _router    = null;

// CANT_ANSWER mirrors the canonical ask_human_server.py CANT_ANSWER constant.
// It is returned (instead of HTTP 500) when the router fails, so the agent
// receives a graceful string and can retry on the next turn.
const CANT_ANSWER = "can't answer (perhaps transient hiccup)";

if (MODE === "ask_human") {
  const kbPath = path.join(TASK_DIR, "blocker_registry.json");
  _router = createHumanInputRouter({
    instanceId:    TASK_UID,
    kbPath,
    trajectoryFile: (ev) => { if (_eventSink !== null) _eventSink.push(ev); },
    workspaceDir:   "/app",
    approvalPolicy: "allow",
    ...(ASK_HUMAN_BASE_URL ? { baseUrl: ASK_HUMAN_BASE_URL } : {}),
    ...(ASK_HUMAN_MODEL    ? { modelId: ASK_HUMAN_MODEL }    : {}),
  });
}

// ── HTTP helpers ──────────────────────────────────────────────────────────────

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data",  (chunk) => { data += chunk; });
    req.on("end",   ()      => {
      try { resolve(JSON.parse(data || "{}")); }
      catch (e) { reject(new Error(`JSON parse error: ${e.message}`)); }
    });
    req.on("error", reject);
  });
}

function sendJson(res, status, body) {
  const text = JSON.stringify(body);
  res.writeHead(status, {
    "Content-Type":   "application/json",
    "Content-Length": Buffer.byteLength(text),
  });
  res.end(text);
}

// ── Request handler ───────────────────────────────────────────────────────────

const server = http.createServer(async (req, res) => {
  try {
    // ── Health check ────────────────────────────────────────────────────────
    if (req.method === "GET" && req.url === "/health") {
      sendJson(res, 200, { ok: true, mode: MODE, uid: TASK_UID });
      return;
    }

    // ── Question routing ────────────────────────────────────────────────────
    if (req.method !== "POST" || req.url !== "/ask") {
      sendJson(res, 404, { error: "not found" });
      return;
    }

    const body = await readBody(req);
    const {
      question          = "",
      options           = [],
      context           = {},
      request_type      = "clarification",
      native_event_type = "adk.ask_human",
      raw_event         = {},
    } = body;

    // ── full_info mode: return "irrelevant question" without calling LLM ────
    // Emit ask_question_full_info_mode event so run_adk.py can count it in
    // num_questions_full_info (mirrors run_codex.mjs / run_claude.mjs behaviour).
    if (MODE !== "ask_human") {
      const ev = {
        type:      "ask_question_full_info_mode",
        timestamp: new Date().toISOString(),
        question:  String(question),
      };
      sendJson(res, 200, {
        resolution:      UNKNOWN_RESOLUTION,
        selected_labels: [UNKNOWN_RESOLUTION],
        blocker_id:      UNKNOWN_BLOCKER_ID,
        status:          "unknown",
        events:          [ev],
      });
      return;
    }

    // ── ask_human mode: route through LLM judge ─────────────────────────────
    const requestEvents = [];
    _eventSink = requestEvents;
    let result;
    try {
      result = await _router.route({
        requestType:      request_type,
        nativeEventType:  native_event_type,
        rawEvent:         raw_event,
        question:         String(question),
        options,
        context,
      });
    } finally {
      _eventSink = null;
    }

    sendJson(res, 200, {
      resolution:      result.resolution      ?? UNKNOWN_RESOLUTION,
      selected_labels: result.selected_labels ?? [result.resolution ?? UNKNOWN_RESOLUTION],
      blocker_id:      result.blocker_id      ?? UNKNOWN_BLOCKER_ID,
      status:          result.status          ?? "unknown",
      events:          requestEvents,
    });

  } catch (err) {
    // Return a 200 with CANT_ANSWER rather than HTTP 500 so the Python harness
    // receives a clean string and mirrors canonical ask_human_server.py's 500
    // handler which also returns {"response": CANT_ANSWER}.
    console.error("[sidecar] request error:", err);
    sendJson(res, 200, {
      resolution:      CANT_ANSWER,
      selected_labels: [CANT_ANSWER],
      blocker_id:      UNKNOWN_BLOCKER_ID,
      status:          "error",
      events:          [],
    });
  }
});

// ── Startup ───────────────────────────────────────────────────────────────────

server.listen(0, "127.0.0.1", () => {
  const { port } = server.address();
  // Signal port to run_adk.py (the Popen'd parent reads this line from stdout).
  process.stdout.write(`SIDECAR_PORT=${port}\n`);
  console.error(`[sidecar] ready  port=${port}  mode=${MODE}  uid=${TASK_UID}`);
});

// ── Shutdown ──────────────────────────────────────────────────────────────────

process.on("SIGTERM", () => {
  server.close(() => {
    console.error("[sidecar] shutdown complete");
    process.exit(0);
  });
});
