import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs/promises";
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
    assert.equal(defaultConfig.model, "litellm/bedrock/qwen.qwen3-32b-v1:0");
    assert.equal(defaultConfig.provider.litellm.options.apiKey, "{env:LITELLM_API_KEY}");
    assert.equal(defaultConfig.provider.litellm.models["bedrock/qwen.qwen3-32b-v1:0"].tool_call, true);

    const config = await opencodeConfig({ model: "gemini/gemini-3.1-pro-preview-customtools" });
    assert.equal(config.model, "litellm/gemini/gemini-3.1-pro-preview-customtools");
    assert.equal(config.provider.litellm.npm, "@ai-sdk/openai-compatible");
    assert.equal(config.provider.litellm.options.baseURL, "http://127.0.0.1:4000/v1");
    assert.equal(config.provider.litellm.options.apiKey, "{env:LITELLM_API_KEY}");
    assert.equal(JSON.stringify(config).includes("unit-test-litellm-key"), false);
    assert.equal(config.provider.litellm.models["gemini/gemini-3.1-pro-preview-customtools"].tool_call, true);
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
