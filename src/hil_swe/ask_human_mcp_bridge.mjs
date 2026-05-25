/**
 * ask_human MCP bridge — used by Codex (and optionally OpenCode) as a subprocess.
 *
 * Minimal stdio JSON-RPC 2.0 MCP server that exposes a single `ask_human`
 * tool. Each call is proxied to ask_human_sidecar.mjs.
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

import process from "node:process";
import readline from "node:readline";

import { CANT_ANSWER } from "../shared/human_input.mjs";
import { sidecarAsk } from "./ask_human_sidecar_client.mjs";
import { richAskHumanToolDescriptionForHarness } from "./constants.mjs";

const SIDECAR_URL = process.env.SIDECAR_URL;
const NATIVE_EVENT_TYPE = String(process.env.NATIVE_EVENT_TYPE || "codex.mcp.ask_human");
if (!SIDECAR_URL) {
  process.stderr.write("[mcp-bridge] ERROR: SIDECAR_URL env var is required\n");
  process.exit(1);
}

const PROTOCOL_VERSION = "2024-11-05";

function _readQuestion(value) {
  if (value == null) return "";
  if (typeof value === "string") {
    if (value.length === 0) return "";
    try {
      const parsed = JSON.parse(value);
      return _readQuestion(parsed);
    } catch {
      return value;
    }
  }
  if (typeof value !== "object") return "";
  if (typeof value.question === "string") {
    return value.question;
  }
  if (value.arguments && typeof value.arguments === "object") {
    const nested = _readQuestion(value.arguments);
    if (nested) return nested;
  }
  if (value.input && typeof value.input === "object") {
    const nested = _readQuestion(value.input);
    if (nested) return nested;
  }
  if (value.ask_human && typeof value.ask_human === "object") {
    const nested = _readQuestion(value.ask_human);
    if (nested) return nested;
  }
  return "";
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

    const question = _readQuestion(args);

    const sidecarResult = await sidecarAsk({
      sidecarUrl: SIDECAR_URL,
      question,
      nativeEventType: NATIVE_EVENT_TYPE,
      rawEvent: { question },
      fallbackSource: `${NATIVE_EVENT_TYPE.replace(/[^a-zA-Z0-9_.-]+/g, "_")}_bridge_error_fallback`,
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
