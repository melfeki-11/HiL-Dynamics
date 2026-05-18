/**
 * ask_human MCP bridge — used by Codex (and optionally OpenCode) as a subprocess.
 *
 * Minimal stdio JSON-RPC 2.0 MCP server that exposes a single `ask_human`
 * tool. Each call is proxied to ask_human_sidecar.mjs unless ask-limit guards
 * (MAX_ASKS_PER_PASS / IRRELEVANT_COOLDOWN) short-circuit locally.
 *
 * OpenCode starts this process as a local MCP subprocess.  Environment vars
 * expected at launch (set by run_opencode.mjs via the MCP config `env` map):
 *
 *   SIDECAR_URL   — full base URL of the sidecar, e.g. http://127.0.0.1:12345
 *
 * Protocol: JSON-RPC 2.0 over stdin/stdout (one JSON object per line).
 * Handles: initialize, notifications/initialized, tools/list, tools/call
 *
 * On any sidecar HTTP failure the bridge returns the canonical CANT_ANSWER
 * string so the agent receives a graceful string and can retry on the next
 * turn — identical to the behaviour of the Python ADK ask_human function.
 */

import fs      from "node:fs";
import http    from "node:http";
import path    from "node:path";
import process from "node:process";
import readline from "node:readline";

import { createAskLimitTracker } from "./ask_limits.mjs";
import { richAskHumanToolDescriptionForHarness } from "./constants.mjs";

const SIDECAR_URL = process.env.SIDECAR_URL;
if (!SIDECAR_URL) {
  process.stderr.write("[mcp-bridge] ERROR: SIDECAR_URL env var is required\n");
  process.exit(1);
}

const CANT_ANSWER      = "can't answer (perhaps transient hiccup)";
const PROTOCOL_VERSION = "2024-11-05";

const TASK_DIR = process.env.TASK_DIR || "/task";
let numBlockersTotal = 0;
try {
  const reg = JSON.parse(
    fs.readFileSync(path.join(TASK_DIR, "blocker_registry.json"), "utf8"),
  );
  numBlockersTotal = (reg.entries || reg.blockers || []).length;
} catch { /* ignore */ }

const askLimitTracker = createAskLimitTracker({ numBlockersTotal });

function fallbackSidecarResult(question = "") {
  const requestId = `codex_customtool_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
  return {
    resolution: CANT_ANSWER,
    selected_labels: [CANT_ANSWER],
    blocker_id: "unknown",
    status: "error",
    events: [
      {
        type: "human_input_raw_event",
        timestamp: new Date().toISOString(),
        request_id: requestId,
        request_type: "clarification",
        native_event_type: "codex.mcp.ask_human",
        question: String(question || ""),
        options: [],
        context: { source: "codex_mcp_bridge_error_fallback" },
        raw_event: { question: String(question || "") },
      },
      {
        type: "human_input_result",
        timestamp: new Date().toISOString(),
        request_id: requestId,
        request_type: "clarification",
        native_event_type: "codex.mcp.ask_human",
        result: {
          resolution: CANT_ANSWER,
          selected_labels: [CANT_ANSWER],
          blocker_id: "unknown",
          status: "error",
        },
      },
    ],
  };
}

// ── JSON-RPC helpers ──────────────────────────────────────────────────────────

function sendMsg(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

function success(id, result) {
  sendMsg({ jsonrpc: "2.0", id, result });
}

function error(id, code, message) {
  sendMsg({ jsonrpc: "2.0", id, error: { code, message } });
}

// ── Sidecar HTTP call ─────────────────────────────────────────────────────────

async function sidecarAsk(question) {
  const payload = JSON.stringify({
    question,
    options:           [],
    context:           {},
    request_type:      "clarification",
    native_event_type: "codex.mcp.ask_human",
    raw_event:         { question },
  });

  return new Promise((resolve) => {
    let urlObj;
    try {
      urlObj = new URL(`${SIDECAR_URL}/ask`);
    } catch {
      resolve(fallbackSidecarResult(question));
      return;
    }

    const options = {
      hostname: urlObj.hostname,
      port:     urlObj.port || 80,
      path:     urlObj.pathname,
      method:   "POST",
      headers:  {
        "Content-Type":   "application/json",
        "Content-Length": Buffer.byteLength(payload),
      },
    };

    const req = http.request(options, (res) => {
      let data = "";
      res.on("data",  (chunk) => { data += chunk; });
      res.on("end",   () => {
        try {
          const parsed = JSON.parse(data);
          resolve({
            resolution: String(parsed.resolution ?? CANT_ANSWER),
            selected_labels: Array.isArray(parsed.selected_labels) ? parsed.selected_labels : [String(parsed.resolution ?? CANT_ANSWER)],
            blocker_id: String(parsed.blocker_id ?? "unknown"),
            status: String(parsed.status ?? "unknown"),
            events: Array.isArray(parsed.events) ? parsed.events : [],
          });
        } catch {
          resolve(fallbackSidecarResult(question));
        }
      });
    });

    req.on("error", () => resolve(fallbackSidecarResult(question)));

    // 20-minute timeout — the LLM judge can be slow on large codebases
    req.setTimeout(1_200_000, () => {
      req.destroy();
      resolve(fallbackSidecarResult(question));
    });

    req.write(payload);
    req.end();
  });
}

// ── MCP tool definition ───────────────────────────────────────────────────────

const ASK_HUMAN_TOOL = {
  name:        "ask_human",
  description: richAskHumanToolDescriptionForHarness() ?? (
    "Ask a human expert a focused question about the implementation requirements. " +
      "Submit only ONE specific question at a time. " +
      "The expert can only answer questions about what the code should do, not how to implement it."
  ),
  inputSchema: {
    type:       "object",
    properties: {
      question: {
        type:        "string",
        description: "A single, specific question for the human expert.",
      },
    },
    required: ["question"],
  },
};

// ── stdin JSON-RPC dispatch ───────────────────────────────────────────────────

const rl = readline.createInterface({ input: process.stdin, terminal: false });

rl.on("line", async (line) => {
  const trimmed = line.trim();
  if (!trimmed) return;

  let msg;
  try {
    msg = JSON.parse(trimmed);
  } catch {
    // Unparseable input — ignore silently (spec allows this for notifications)
    return;
  }

  const id     = msg.id ?? null;
  const method = msg.method ?? "";

  // Notifications have no id — handle without responding
  if (method === "notifications/initialized" || method === "notifications/cancelled") {
    return;
  }

  if (method === "initialize") {
    success(id, {
      protocolVersion: PROTOCOL_VERSION,
      capabilities:    { tools: {} },
      serverInfo:      { name: "ask_human_bridge", version: "1.0.0" },
    });
    return;
  }

  if (method === "tools/list") {
    success(id, { tools: [ASK_HUMAN_TOOL] });
    return;
  }

  if (method === "tools/call") {
    const toolName = msg.params?.name ?? "";
    const args     = msg.params?.arguments ?? {};

    if (toolName !== "ask_human") {
      error(id, -32601, `Unknown tool: ${toolName}`);
      return;
    }

    const question = String(args.question ?? "");

    const gate = askLimitTracker.checkBeforeJudge();
    if (gate.shortCircuit) {
      const ts = new Date().toISOString();
      const synth = {
        resolution: gate.responseText,
        selected_labels: [gate.responseText],
        blocker_id: "unknown",
        status: "ask_limit_suppressed",
        events: [
          {
            type: "ask_human_suppressed",
            timestamp: ts,
            reason: gate.reason,
            question,
            sdk: "codex_mcp",
          },
        ],
      };
      success(id, {
        content: [{ type: "text", text: gate.responseText }],
        structuredContent: synth,
        isError: false,
      });
      return;
    }

    askLimitTracker.notifyRoutedToJudge();

    let sidecarResult;
    try {
      sidecarResult = await sidecarAsk(question);
    } catch {
      sidecarResult = fallbackSidecarResult(question);
    }
    askLimitTracker.recordJudgeResolution(sidecarResult.resolution, {
      blockerId: sidecarResult.blocker_id,
      status: sidecarResult.status,
    });

    success(id, {
      content: [{ type: "text", text: String(sidecarResult.resolution ?? CANT_ANSWER) }],
      structuredContent: {
        resolution: String(sidecarResult.resolution ?? CANT_ANSWER),
        selected_labels: Array.isArray(sidecarResult.selected_labels) ? sidecarResult.selected_labels : [String(sidecarResult.resolution ?? CANT_ANSWER)],
        blocker_id: String(sidecarResult.blocker_id ?? "unknown"),
        status: String(sidecarResult.status ?? "unknown"),
        events: Array.isArray(sidecarResult.events) ? sidecarResult.events : [],
      },
      isError: false,
    });
    return;
  }

  // Method not found — respond with error for requests (id !== null), ignore for notifications
  if (id !== null) {
    error(id, -32601, `Method not found: ${method}`);
  }
});

rl.on("close", () => {
  // stdin closed — OpenCode has exited; shut down cleanly
  process.exit(0);
});

process.stderr.write(`[mcp-bridge] ready  sidecar=${SIDECAR_URL}\n`);
