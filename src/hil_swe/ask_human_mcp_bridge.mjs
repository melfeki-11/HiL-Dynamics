/**
 * ask_human MCP bridge for run_opencode.mjs
 *
 * Minimal stdio JSON-RPC 2.0 MCP server that exposes a single `ask_human`
 * tool to OpenCode (which can only call external tools via MCP).  Each call
 * is proxied to the existing ask_human_sidecar.mjs HTTP server.
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

import http    from "node:http";
import process from "node:process";
import readline from "node:readline";

const SIDECAR_URL = process.env.SIDECAR_URL;
if (!SIDECAR_URL) {
  process.stderr.write("[mcp-bridge] ERROR: SIDECAR_URL env var is required\n");
  process.exit(1);
}

const CANT_ANSWER      = "can't answer (perhaps transient hiccup)";
const PROTOCOL_VERSION = "2024-11-05";

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
    native_event_type: "opencode.ask_human",
    raw_event:         { question },
  });

  return new Promise((resolve) => {
    let urlObj;
    try {
      urlObj = new URL(`${SIDECAR_URL}/ask`);
    } catch {
      resolve(CANT_ANSWER);
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
          resolve(String(parsed.resolution ?? CANT_ANSWER));
        } catch {
          resolve(CANT_ANSWER);
        }
      });
    });

    req.on("error", () => resolve(CANT_ANSWER));

    // 20-minute timeout — the LLM judge can be slow on large codebases
    req.setTimeout(1_200_000, () => {
      req.destroy();
      resolve(CANT_ANSWER);
    });

    req.write(payload);
    req.end();
  });
}

// ── MCP tool definition ───────────────────────────────────────────────────────

const ASK_HUMAN_TOOL = {
  name:        "ask_human",
  description: (
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

    let resolution;
    try {
      resolution = await sidecarAsk(question);
    } catch {
      resolution = CANT_ANSWER;
    }

    success(id, {
      content: [{ type: "text", text: resolution }],
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
