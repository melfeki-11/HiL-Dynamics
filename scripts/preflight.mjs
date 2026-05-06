#!/usr/bin/env node
import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import {
  DEFAULT_ASK_HUMAN_MODEL,
  DEFAULT_CLAUDE_MODEL,
  DEFAULT_CODEX_MODEL,
  DEFAULT_LITELLM_CREDENTIALS_FILE,
  DEFAULT_OPENCODE_MODEL,
  ensureLiteLLMEnvLoaded,
  getLiteLLMKey,
  getResponsesBaseUrl,
  rootDir,
} from "../src/shared/config.mjs";
import { availableOpencodePorts, opencodePortRange } from "../src/harnesses/opencode/server_pool.mjs";

function parseArgs(argv) {
  const args = {
    models: [DEFAULT_ASK_HUMAN_MODEL, DEFAULT_CLAUDE_MODEL, DEFAULT_CODEX_MODEL, DEFAULT_OPENCODE_MODEL],
    skipModels: false,
    skipDocker: false,
    requiredOpencodePorts: Number(process.env.PREFLIGHT_REQUIRED_OPENCODE_PORTS || process.env.HARNESS_MAX_CONCURRENCY || 1),
    tasksDir: process.env.HIL_SWE_TASKS_DIR || "",
  };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--model") args.models.push(argv[++i]);
    else if (arg === "--only-model") args.models = [argv[++i]];
    else if (arg === "--skip-models") args.skipModels = true;
    else if (arg === "--skip-docker") args.skipDocker = true;
    else if (arg === "--required-opencode-ports") args.requiredOpencodePorts = Number(argv[++i]);
    else if (arg === "--tasks-dir") args.tasksDir = argv[++i];
    else throw new Error(`Unknown argument: ${arg}`);
  }
  args.models = [...new Set(args.models.filter(Boolean))];
  return args;
}

async function checkCapacity(args) {
  const range = opencodePortRange();
  const availablePorts = await availableOpencodePorts();
  const requiredPorts = Number.isInteger(args.requiredOpencodePorts) && args.requiredOpencodePorts > 0 ? args.requiredOpencodePorts : 1;
  if (availablePorts < requiredPorts) {
    throw new Error(`OpenCode port capacity too small: need ${requiredPorts}, available ${availablePorts}, range ${range.start}-${range.end}, lock dir ${range.lock_dir}`);
  }
  const freeMemoryGb = os.freemem() / 1024 ** 3;
  if (freeMemoryGb < 12) throw new Error(`Available memory too low for one generation worker: ${freeMemoryGb.toFixed(1)} GB`);
  console.log(`OpenCode ports: ${availablePorts}/${range.size} available in ${range.start}-${range.end}`);
  console.log(`Available memory: ${freeMemoryGb.toFixed(1)} GB`);
  try {
    const limits = await run("sh", ["-lc", "ulimit -n"]);
    const openFiles = Number(limits.stdout.trim());
    if (Number.isFinite(openFiles) && openFiles < 4096) throw new Error(`Open-file limit too low: ${openFiles}`);
    console.log(`Open-file limit: ${limits.stdout.trim()}`);
  } catch (error) {
    throw new Error(`Could not verify open-file limit: ${error.message}`);
  }
}

async function checkDockerDisk() {
  const dockerRoot = process.env.DOCKER_ROOT || "/var/lib/docker";
  const stat = await fs.statfs(dockerRoot);
  const availableGb = Number(stat.bavail * stat.bsize) / 1024 ** 3;
  if (availableGb < Number(process.env.PREFLIGHT_MIN_DOCKER_FREE_GB || 20)) {
    throw new Error(`Docker filesystem free space too low: ${availableGb.toFixed(1)} GB at ${dockerRoot}`);
  }
  console.log(`Docker filesystem free: ${availableGb.toFixed(1)} GB at ${dockerRoot}`);
}

async function metadataImageNames(tasksDir) {
  if (!tasksDir) return [];
  const entries = await fs.readdir(tasksDir, { withFileTypes: true });
  const images = [];
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    const metadataPath = path.join(tasksDir, entry.name, "metadata.json");
    try {
      const metadata = JSON.parse(await fs.readFile(metadataPath, "utf8"));
      if (metadata.image_name) images.push(String(metadata.image_name));
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
  }
  return [...new Set(images)];
}

async function checkTaskImages(tasksDir) {
  const images = await metadataImageNames(tasksDir);
  if (!images.length) return;
  const missing = [];
  for (const image of images) {
    try {
      await run("docker", ["image", "inspect", image]);
    } catch {
      missing.push(image);
    }
  }
  if (missing.length) {
    throw new Error(`Missing official HiL-SWE Docker image(s); pull/prebuild before scale:\n${missing.join("\n")}`);
  }
  console.log(`Official task images present: ${images.length}`);
}

async function run(command, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { ...options, stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout?.on("data", (chunk) => { stdout += chunk.toString(); });
    child.stderr?.on("data", (chunk) => { stderr += chunk.toString(); });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code === 0) resolve({ stdout, stderr });
      else reject(new Error(`${command} ${args.join(" ")} failed with ${code}: ${stderr || stdout}`));
    });
  });
}

async function checkModels(models) {
  const token = await getLiteLLMKey();
  const base = getResponsesBaseUrl().replace(/\/+$/, "");
  const url = `${base}/models`;
  const response = await fetch(url, { headers: { authorization: `Bearer ${token}` } });
  if (!response.ok) throw new Error(`LiteLLM model probe failed ${response.status}: ${(await response.text()).slice(0, 300)}`);
  const parsed = await response.json();
  const ids = new Set((parsed.data || []).map((item) => String(item.id || "")));
  const missing = models.filter((model) => !ids.has(model));
  if (missing.length) {
    const failed = [];
    for (const model of missing) {
      try {
        await probeChatCompletion({ base, token, model });
      } catch (error) {
        failed.push(`${model}: ${String(error?.message || error).slice(0, 300)}`);
      }
    }
    if (failed.length) {
      throw new Error(`LiteLLM endpoint is reachable, but these configured models failed live probes:\n${failed.join("\n")}`);
    }
    console.log(`LiteLLM models not listed but live-probed successfully: ${missing.join(", ")}`);
  }
}

async function probeChatCompletion({ base, token, model }) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(new Error(`model probe timeout for ${model}`)), 30000);
  try {
    const body = {
      model,
      messages: [{ role: "user", content: "Reply with ok." }],
      temperature: 0,
      max_tokens: 16,
    };
    const response = await fetch(`${base}/chat/completions`, {
      method: "POST",
      headers: {
        authorization: `Bearer ${token}`,
        "content-type": "application/json",
      },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    const text = await response.text();
    if (response.ok) return;
    if (/temperature/i.test(text) && /unsupported|does not support|only.*default|deprecated/i.test(text)) {
      delete body.temperature;
      const retry = await fetch(`${base}/chat/completions`, {
        method: "POST",
        headers: {
          authorization: `Bearer ${token}`,
          "content-type": "application/json",
        },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      const retryText = await retry.text();
      if (retry.ok) return;
      if (/max_tokens|output limit|finish the message/i.test(retryText)) {
        body.max_tokens = 64;
        const secondRetry = await fetch(`${base}/chat/completions`, {
          method: "POST",
          headers: {
            authorization: `Bearer ${token}`,
            "content-type": "application/json",
          },
          body: JSON.stringify(body),
          signal: controller.signal,
        });
        const secondRetryText = await secondRetry.text();
        if (secondRetry.ok) return;
        throw new Error(`HTTP ${secondRetry.status}: ${secondRetryText.slice(0, 300)}`);
      }
      throw new Error(`HTTP ${retry.status}: ${retryText.slice(0, 300)}`);
    }
    if (/max_tokens|output limit|finish the message/i.test(text)) {
      body.max_tokens = 64;
      const retry = await fetch(`${base}/chat/completions`, {
        method: "POST",
        headers: {
          authorization: `Bearer ${token}`,
          "content-type": "application/json",
        },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      const retryText = await retry.text();
      if (retry.ok) return;
      throw new Error(`HTTP ${retry.status}: ${retryText.slice(0, 300)}`);
    }
    throw new Error(`HTTP ${response.status}: ${text.slice(0, 300)}`);
  } finally {
    clearTimeout(timeout);
  }
}

async function checkOpenCodeCli() {
  const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), "trust-horizon-opencode-preflight-"));
  const env = {
    ...process.env,
    HOME: path.join(tempRoot, "home"),
    XDG_CONFIG_HOME: path.join(tempRoot, "config"),
    XDG_DATA_HOME: path.join(tempRoot, "data"),
    PATH: `${path.join(rootDir, "node_modules", ".bin")}${path.delimiter}${process.env.PATH || ""}`,
  };
  await fs.mkdir(env.HOME, { recursive: true });
  const result = await run("opencode", ["--version"], { env });
  return result.stdout.trim();
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const loaded = ensureLiteLLMEnvLoaded({ required: true });
  console.log(`LiteLLM env file: ${loaded.path || DEFAULT_LITELLM_CREDENTIALS_FILE}`);
  console.log(`Loaded env keys: ${loaded.keys.length}`);
  console.log(`LiteLLM base URL: ${getResponsesBaseUrl()}`);
  if (!args.skipModels) {
    await checkModels(args.models);
    console.log(`LiteLLM models reachable: ${args.models.join(", ")}`);
  }
  await checkCapacity(args);
  const opencodeVersion = await checkOpenCodeCli();
  console.log(`OpenCode CLI: ${opencodeVersion}`);
  if (!args.skipDocker) {
    const docker = await run("docker", ["version", "--format", "{{.Server.Version}}"]);
    console.log(`Docker server: ${docker.stdout.trim()}`);
    await checkDockerDisk();
    if (args.tasksDir) await checkTaskImages(args.tasksDir);
  }
  console.log("Preflight passed.");
}

main().catch((error) => {
  console.error(error?.message || error);
  process.exit(1);
});
