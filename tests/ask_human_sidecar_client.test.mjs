import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { spawn } from "node:child_process";

import { sidecarAsk } from "../src/hil_swe/ask_human_sidecar_client.mjs";

async function startFakeSidecar(handler) {
  const requests = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", () => {
      const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString("utf8")) : {};
      requests.push({ method: req.method, url: req.url, body });
      const responseBody = handler({ req, body }) || {};
      const text = JSON.stringify(responseBody);
      res.writeHead(200, {
        "content-type": "application/json",
        "content-length": Buffer.byteLength(text),
      });
      res.end(text);
    });
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  return {
    requests,
    url: `http://127.0.0.1:${server.address().port}`,
    close: () => new Promise((resolve) => server.close(resolve)),
  };
}

function readJsonRpcLine(child, timeoutMs = 5000) {
  return new Promise((resolve, reject) => {
    let stdout = "";
    const timeout = setTimeout(() => {
      cleanup();
      reject(new Error(`timed out waiting for bridge response. stdout=${stdout}`));
    }, timeoutMs);
    const onData = (chunk) => {
      stdout += String(chunk);
      const idx = stdout.indexOf("\n");
      if (idx < 0) return;
      const line = stdout.slice(0, idx).trim();
      cleanup();
      resolve(JSON.parse(line));
    };
    const onExit = (code) => {
      cleanup();
      reject(new Error(`bridge exited early with code ${code}. stdout=${stdout}`));
    };
    function cleanup() {
      clearTimeout(timeout);
      child.stdout.off("data", onData);
      child.off("exit", onExit);
    }
    child.stdout.on("data", onData);
    child.once("exit", onExit);
  });
}

test("sidecarAsk forwards native question metadata and returns canonical shape", async () => {
  const fake = await startFakeSidecar(({ body }) => ({
    resolution: "Use /tmp/output.json",
    selected_labels: ["Use /tmp/output.json"],
    blocker_id: "output_path",
    status: "answered",
    events: [
      {
        type: "human_input_raw_event",
        request_id: "req-1",
        request_type: body.request_type,
        native_event_type: body.native_event_type,
        question: body.question,
      },
      {
        type: "human_input_result",
        request_id: "req-1",
        request_type: body.request_type,
        native_event_type: body.native_event_type,
        result: { status: "answered", blocker_id: "output_path" },
      },
    ],
  }));

  try {
    const result = await sidecarAsk({
      sidecarUrl: fake.url,
      question: "Where should the output be written?",
      requestType: "clarification",
      nativeEventType: "claude.AskUserQuestion.canUseTool",
      rawEvent: { id: "q1" },
      context: { source: "unit" },
    });

    assert.equal(fake.requests.length, 1);
    assert.equal(fake.requests[0].url, "/ask");
    assert.equal(fake.requests[0].body.question, "Where should the output be written?");
    assert.equal(fake.requests[0].body.native_event_type, "claude.AskUserQuestion.canUseTool");
    assert.equal(fake.requests[0].body.context.source, "unit");
    assert.equal(result.status, "answered");
    assert.equal(result.blocker_id, "output_path");
    assert.equal(result.events.length, 2);
  } finally {
    await fake.close();
  }
});

test("MCP bridge routes ask_human calls through the same sidecar contract", async () => {
  const fake = await startFakeSidecar(({ body }) => ({
    resolution: "Use the explicit threshold from the task note.",
    selected_labels: ["Use the explicit threshold from the task note."],
    blocker_id: "threshold",
    status: "answered",
    events: [
      {
        type: "human_input_raw_event",
        request_id: "mcp-1",
        request_type: body.request_type,
        native_event_type: body.native_event_type,
        question: body.question,
      },
    ],
  }));
  const bridge = spawn(process.execPath, ["src/hil_swe/ask_human_mcp_bridge.mjs"], {
    env: { ...process.env, SIDECAR_URL: fake.url },
    stdio: ["pipe", "pipe", "pipe"],
  });

  try {
    bridge.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id: 1, method: "initialize", params: {} })}\n`);
    const init = await readJsonRpcLine(bridge);
    assert.equal(init.result.serverInfo.name, "ask_human_bridge");

    bridge.stdin.write(`${JSON.stringify({
      jsonrpc: "2.0",
      id: 2,
      method: "tools/call",
      params: {
        name: "ask_human",
        arguments: { question: "Which threshold should I use?" },
      },
    })}\n`);
    const call = await readJsonRpcLine(bridge);
    assert.equal(call.result.content[0].text, "Use the explicit threshold from the task note.");
    assert.equal(call.result.structuredContent.blocker_id, "threshold");
    assert.equal(fake.requests.length, 1);
    assert.equal(fake.requests[0].body.native_event_type, "codex.mcp.ask_human");
    assert.equal(fake.requests[0].body.question, "Which threshold should I use?");
  } finally {
    bridge.kill("SIGTERM");
    await fake.close();
  }
});
