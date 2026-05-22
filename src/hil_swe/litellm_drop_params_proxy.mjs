/**
 * litellm_drop_params_proxy.mjs
 *
 * Minimal transparent HTTP proxy used by run_opencode.mjs.
 * Its sole job: strip "tool_choice" from ALL POST request bodies and inject
 * "drop_params: true" before forwarding to the real LiteLLM proxy.
 *
 * Why ALL POST paths (not just /v1/chat/completions):
 *   OpenCode's built-in "openai" provider sends requests to /v1/responses
 *   (the OpenAI Responses API), NOT /v1/chat/completions.  fireworks_ai
 *   models reject "tool_choice" on every endpoint, so we must strip it
 *   from ALL POST bodies, not just the chat completions path.
 *
 * All GET/HEAD/etc. requests and streaming SSE responses are forwarded
 * unchanged.
 *
 * Environment variables (required):
 *   REAL_LITELLM_URL   — upstream LiteLLM proxy base URL (no trailing slash)
 *   LITELLM_API_KEY    — Bearer token forwarded in Authorization header
 *
 * Stdout:
 *   PROXY_PORT=<n>     — port the local server is listening on
 */

import http  from "node:http";
import https from "node:https";

const REAL_URL = (process.env.REAL_LITELLM_URL || "").replace(/\/+$/, "");
if (!REAL_URL) {
  process.stderr.write("[llm-proxy] ERROR: REAL_LITELLM_URL env var is required\n");
  process.exit(1);
}

let upstreamBase;
try {
  upstreamBase = new URL(REAL_URL);
} catch {
  process.stderr.write(`[llm-proxy] ERROR: REAL_LITELLM_URL is not a valid URL: ${REAL_URL}\n`);
  process.exit(1);
}
const upstreamIsHttps = upstreamBase.protocol === "https:";
const upstreamTransport = upstreamIsHttps ? https : http;
const TARGET_MODEL = (process.env.LITELLM_PROXY_TARGET_MODEL || "").trim().toLowerCase();

const stats = {
  llm_call_count: 0,
  input_tokens: 0,
  output_tokens: 0,
  total_tokens: 0,
  error_count: 0,
  status_counts: {},
  stripped_params: {},
  recent_errors: [],
};

function isGeminiModel(model) {
  return model.startsWith("gemini/") || model.startsWith("google/gemini") || model.includes("/gemini");
}

function addRecentError(message) {
  const text = String(message || "").slice(0, 2000);
  if (!text) return;
  stats.recent_errors.push(text);
  if (stats.recent_errors.length > 20) stats.recent_errors.shift();
}

function recordStrippedParam(name) {
  stats.stripped_params[name] = (stats.stripped_params[name] || 0) + 1;
  process.stderr.write(`[llm-proxy] stripped unsupported param: ${name}\n`);
}

function stripParam(obj, name) {
  if (Object.prototype.hasOwnProperty.call(obj, name)) {
    delete obj[name];
    recordStrippedParam(name);
  }
}

function numericField(obj, names) {
  for (const name of names) {
    const value = obj?.[name];
    if (Number.isFinite(value)) return Number(value);
  }
  return null;
}

function collectUsage(value, seen = new Set()) {
  if (!value || typeof value !== "object") return;
  if (seen.has(value)) return;
  seen.add(value);

  const usage = value.usage && typeof value.usage === "object" ? value.usage : value;
  const input = numericField(usage, ["input_tokens", "prompt_tokens", "inputTokens", "promptTokens"]);
  const output = numericField(usage, ["output_tokens", "completion_tokens", "outputTokens", "completionTokens"]);
  const total = numericField(usage, ["total_tokens", "totalTokens"]);
  if (input !== null || output !== null || total !== null) {
    stats.input_tokens += input || 0;
    stats.output_tokens += output || 0;
    stats.total_tokens += total || ((input || 0) + (output || 0));
  }

  if (Array.isArray(value)) {
    for (const item of value) collectUsage(item, seen);
    return;
  }
  for (const [key, item] of Object.entries(value)) {
    if (usage !== value && key === "usage") continue;
    collectUsage(item, seen);
  }
}

function collectUsageFromBody(buffer) {
  const text = buffer.toString("utf8");
  if (!text.trim()) return;
  try {
    collectUsage(JSON.parse(text));
    return;
  } catch {
    // Maybe an SSE stream. Parse each data: line independently.
  }
  for (const line of text.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed.startsWith("data:")) continue;
    const payload = trimmed.slice("data:".length).trim();
    if (!payload || payload === "[DONE]") continue;
    try {
      collectUsage(JSON.parse(payload));
    } catch {
      // Ignore non-JSON SSE data.
    }
  }
}

const server = http.createServer((req, res) => {
  if (req.method === "GET" && req.url === "/__stats") {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify(stats));
    return;
  }

  // Collect the full request body before deciding how to patch it.
  const chunks = [];
  req.on("data", (c) => chunks.push(c));
  req.on("end",  () => {
    const rawBody = Buffer.concat(chunks);

    // Patch ALL POST requests: strip tool_choice + inject drop_params.
    // OpenCode's built-in "openai" provider sends to /v1/responses (Responses API),
    // not /v1/chat/completions, so we must patch every POST, not just one path.
    let forwardBody = rawBody;
    if (req.method === "POST") {
      try {
        const parsed = JSON.parse(rawBody.toString("utf8"));
        stats.llm_call_count += 1;
        // Remove tool_choice — fireworks_ai and other models reject it.
        stripParam(parsed, "tool_choice");
        // Gemini via LiteLLM rejects OpenAI-style reasoning params on the
        // chat-completions route. Keep configured effort in harness metadata;
        // do not let the transport fail on unsupported provider parameters.
        if (isGeminiModel(TARGET_MODEL || String(parsed.model || ""))) {
          stripParam(parsed, "reasoning_effort");
          stripParam(parsed, "reasoning");
        }
        // Ask LiteLLM to silently drop any other unsupported params.
        parsed.drop_params = true;
        forwardBody = Buffer.from(JSON.stringify(parsed), "utf8");
      } catch {
        // Body is not JSON (unexpected but possible) — forward unchanged.
      }
    }

    // Build the upstream request options.
    const upstreamPath = req.url + (req.url.includes("?") ? "" : "");
    const forwardHeaders = { ...req.headers };
    // Fix the Host header to point at the upstream.
    forwardHeaders["host"] = upstreamBase.host;
    // Update Content-Length if we modified the body.
    if (forwardBody !== rawBody) {
      forwardHeaders["content-length"] = String(forwardBody.length);
    }

    const upstreamOpts = {
      hostname: upstreamBase.hostname,
      port:     upstreamBase.port
                  ? Number(upstreamBase.port)
                  : (upstreamIsHttps ? 443 : 80),
      path:     upstreamPath,
      method:   req.method,
      headers:  forwardHeaders,
      // Don't enforce a timeout — LLM responses can be slow.
      timeout:  0,
    };

    const proxyReq = upstreamTransport.request(upstreamOpts, (proxyRes) => {
      const status = proxyRes.statusCode ?? 502;
      stats.status_counts[String(status)] = (stats.status_counts[String(status)] || 0) + 1;
      res.writeHead(proxyRes.statusCode ?? 502, proxyRes.headers);
      const responseChunks = [];
      proxyRes.on("data", (chunk) => {
        responseChunks.push(chunk);
        res.write(chunk);
      });
      proxyRes.on("end", () => {
        const body = Buffer.concat(responseChunks);
        collectUsageFromBody(body);
        if (status >= 400) {
          stats.error_count += 1;
          addRecentError(`HTTP ${status} ${body.toString("utf8").slice(0, 1200)}`);
        }
        res.end();
      });
    });

    proxyReq.on("error", (err) => {
      stats.error_count += 1;
      addRecentError(`proxy error: ${err.message}`);
      process.stderr.write(`[llm-proxy] upstream error: ${err.message}\n`);
      if (!res.headersSent) {
        res.writeHead(502, { "Content-Type": "application/json" });
      }
      res.end(JSON.stringify({ error: { message: `proxy error: ${err.message}` } }));
    });

    proxyReq.write(forwardBody);
    proxyReq.end();
  });

  req.on("error", (err) => {
    process.stderr.write(`[llm-proxy] request error: ${err.message}\n`);
  });
});

server.listen(0, "127.0.0.1", () => {
  const port = server.address().port;
  // Announce port on stdout for the parent process to parse.
  process.stdout.write(`PROXY_PORT=${port}\n`);
  process.stderr.write(`[llm-proxy] ready  upstream=${REAL_URL}  port=${port}\n`);
});
