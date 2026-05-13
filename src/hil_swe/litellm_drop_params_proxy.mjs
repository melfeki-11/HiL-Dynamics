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

const server = http.createServer((req, res) => {
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
        // Remove tool_choice — fireworks_ai and other models reject it.
        delete parsed.tool_choice;
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
      res.writeHead(proxyRes.statusCode ?? 502, proxyRes.headers);
      // Pipe the response directly — handles both JSON and SSE streaming.
      proxyRes.pipe(res, { end: true });
    });

    proxyReq.on("error", (err) => {
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
