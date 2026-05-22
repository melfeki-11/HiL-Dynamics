import path from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { createOpencode } from "@opencode-ai/sdk";
import { archiveExistingAttempt } from "../../shared/attempts.mjs";
import { DEFAULT_OPENCODE_MODEL, opencodeConfig, opencodeEnv, rootDir } from "../../shared/config.mjs";
import { clarificationInstruction, promptForInstance, publicMetadata } from "../../shared/dataset.mjs";
import { attemptWorkspace, cloneCheckout, diff } from "../../shared/git.mjs";
import { createHumanInputRouter } from "../../shared/human_input.mjs";
import { appendJsonl, ensureDir, pathExists, writeJson, writeJsonAtomic, writeText } from "../../shared/io.mjs";
import { redactString } from "../../shared/redact.mjs";
import { acquirePort, releasePort } from "./server_pool.mjs";

const execFileAsync = promisify(execFile);
let processEnvMutationTail = Promise.resolve();

export const harness = {
  name: "opencode",
  defaultModel: DEFAULT_OPENCODE_MODEL,
  runAttempt,
};

function providerModel(model) {
  const value = model || DEFAULT_OPENCODE_MODEL;
  return { providerID: "litellm", modelID: value.startsWith("litellm/") ? value.slice("litellm/".length) : value };
}

async function withTemporaryProcessEnv(env, fn) {
  const previousTail = processEnvMutationTail;
  let release;
  const currentTail = new Promise((resolve) => {
    release = resolve;
  });
  processEnvMutationTail = previousTail.then(() => currentTail, () => currentTail);
  await previousTail.catch(() => {});
  const previous = new Map();
  for (const [key, value] of Object.entries(env)) {
    previous.set(key, process.env[key]);
    process.env[key] = value;
  }
  try {
    return await fn();
  } finally {
    for (const [key, value] of previous.entries()) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    release();
    if (processEnvMutationTail === currentTail) processEnvMutationTail = Promise.resolve();
  }
}

function unwrap(result, label) {
  if (result?.error) throw new Error(`${label}: ${JSON.stringify(result.error)}`);
  if (result?.data !== undefined) return result.data;
  return result;
}

async function listProcesses() {
  const { stdout } = await execFileAsync("ps", ["-eo", "pid=,ppid=,args="], { maxBuffer: 8 * 1024 * 1024 });
  return stdout
    .split(/\r?\n/)
    .map((line) => {
      const match = line.trim().match(/^(\d+)\s+(\d+)\s+(.*)$/);
      if (!match) return null;
      return { pid: Number(match[1]), ppid: Number(match[2]), args: match[3] };
    })
    .filter(Boolean);
}

function openCodeServerPids(processes, port) {
  const portFlags = [`--port=${port}`, `--port ${port}`];
  const roots = processes
    .filter((proc) => proc.pid !== process.pid)
    .filter((proc) => proc.args.includes("opencode") && proc.args.includes(" serve"))
    .filter((proc) => portFlags.some((flag) => proc.args.includes(flag)))
    .map((proc) => proc.pid);
  const byParent = new Map();
  for (const proc of processes) {
    if (!byParent.has(proc.ppid)) byParent.set(proc.ppid, []);
    byParent.get(proc.ppid).push(proc.pid);
  }
  const all = new Set(roots);
  const stack = [...roots];
  while (stack.length) {
    const pid = stack.pop();
    for (const child of byParent.get(pid) || []) {
      if (child === process.pid || all.has(child)) continue;
      all.add(child);
      stack.push(child);
    }
  }
  return [...all];
}

async function sleep(ms) {
  await new Promise((resolve) => setTimeout(resolve, ms));
}

async function terminatePids(pids, signal) {
  for (const pid of [...pids].sort((a, b) => b - a)) {
    try {
      process.kill(pid, signal);
    } catch (error) {
      if (error?.code !== "ESRCH") throw error;
    }
  }
}

async function cleanupOpenCodeServer({ port, trajectoryFile }) {
  if (port === null || port === undefined) return;
  let processes = await listProcesses();
  let pids = openCodeServerPids(processes, port);
  if (!pids.length) return;
  await appendJsonl(trajectoryFile, {
    type: "opencode_server_cleanup",
    timestamp: new Date().toISOString(),
    port,
    pids,
    signal: "SIGTERM",
  });
  await terminatePids(pids, "SIGTERM");
  await sleep(Number(process.env.OPENCODE_SERVER_TERM_GRACE_MS || 1000));
  processes = await listProcesses();
  pids = openCodeServerPids(processes, port);
  if (!pids.length) return;
  await appendJsonl(trajectoryFile, {
    type: "opencode_server_cleanup",
    timestamp: new Date().toISOString(),
    port,
    pids,
    signal: "SIGKILL",
  });
  await terminatePids(pids, "SIGKILL");
}

async function consumeEvents({ client, sessionId, trajectoryFile, router, abortSignal }) {
  const streamController = new AbortController();
  const abort = () => streamController.abort(abortSignal?.reason || new Error("OpenCode event stream aborted"));
  abortSignal?.addEventListener("abort", abort, { once: true });
  try {
    const events = await client.event.subscribe({
      signal: streamController.signal,
      sseMaxRetryAttempts: 0,
    });
    for await (const event of events.stream) {
      await appendJsonl(trajectoryFile, { type: "opencode_event", timestamp: new Date().toISOString(), event });
      const payload = event?.payload || event;
      if (payload?.type === "permission.updated" && payload.properties?.sessionID === sessionId) {
        await answerPermission({ client, sessionId, permission: payload.properties, trajectoryFile, router });
      }
      if (payload?.type === "session.error" && (!payload.properties?.sessionID || payload.properties.sessionID === sessionId)) {
        throw new Error(`OpenCode session error: ${JSON.stringify(payload.properties?.error || payload.properties || payload)}`);
      }
    }
  } catch (error) {
    if (!streamController.signal.aborted) throw error;
  } finally {
    abortSignal?.removeEventListener("abort", abort);
  }
}

async function answerPermission({ client, sessionId, permission, trajectoryFile, router }) {
  const question = permission.title || `Approve OpenCode permission ${permission.type || ""}`;
  const routed = await router.routeApproval({
    requestType: "permission",
    nativeEventType: "opencode.permission.updated",
    rawEvent: permission,
    question,
    context: {
      toolName: permission.type,
      pattern: permission.pattern,
      input: permission.metadata || {},
    },
  });
  const response = routed.approval.allowed ? "once" : "reject";
  await appendJsonl(trajectoryFile, {
    type: "opencode_permission_response",
    timestamp: new Date().toISOString(),
    permission_id: permission.id,
    session_id: sessionId,
    response,
  });
  await client.postSessionIdPermissionsPermissionId({
    path: { id: sessionId, permissionID: permission.id },
    body: { response },
  });
}

async function flushSessionMessages({ client, sessionId, trajectoryFile }) {
  const messages = unwrap(await client.session.messages({ path: { id: sessionId } }), "OpenCode session.messages");
  if (!Array.isArray(messages)) return;
  for (const item of messages) {
    if (item?.info) {
      await appendJsonl(trajectoryFile, {
        type: "opencode_event",
        timestamp: new Date().toISOString(),
        event: {
          type: "message.updated",
          properties: { info: item.info },
          snapshot: true,
        },
      });
    }
    for (const part of item?.parts || []) {
      await appendJsonl(trajectoryFile, {
        type: "opencode_event",
        timestamp: new Date().toISOString(),
        event: {
          type: "message.part.updated",
          properties: { part },
          snapshot: true,
        },
      });
    }
  }
}

async function waitForOpenCodeIdle({ client, sessionId, workspaceDir, trajectoryFile, abortSignal }) {
  const intervalMs = Number(process.env.OPENCODE_STATUS_POLL_MS || 5000);
  let lastStatus = null;
  while (true) {
    if (abortSignal?.aborted) throw abortSignal.reason || new Error("OpenCode attempt aborted");
    const statuses = unwrap(
      await client.session.status({
        query: { directory: workspaceDir },
      }),
      "OpenCode session.status"
    );
    const status = statuses?.[sessionId] || null;
    const serialized = JSON.stringify(status);
    if (serialized !== lastStatus) {
      lastStatus = serialized;
      await appendJsonl(trajectoryFile, {
        type: "opencode_session_status",
        timestamp: new Date().toISOString(),
        session_id: sessionId,
        status,
      });
    }
    if (status?.type === "idle") return;
    await sleep(intervalMs);
  }
}

function mcpRuntimeEnv({ instanceId, args, trajectoryFile, workspaceDir }) {
  return {
    HIL_DYNAMICS_INSTANCE_ID: instanceId,
    HARNESS_HUMAN_KB: args.humanKb || "",
    HARNESS_ASK_HUMAN_CACHE: args.askHumanCache || "",
    HARNESS_ASK_HUMAN_REPLAY: args.askHumanReplay ? "true" : "false",
    HARNESS_TRAJECTORY_FILE: trajectoryFile,
    HARNESS_WORKSPACE_DIR: workspaceDir,
    HARNESS_APPROVAL_POLICY_ROUTER: args.approvalPolicyRouter || "safe-looking",
    ASK_HUMAN_MODEL: args.askHumanModel,
    ASK_HUMAN_SEED: String(args.askHumanSeed),
  };
}

function mcpConfig({ args }) {
  if (!args.humanKb) return {};
  return {
    human_input: {
      type: "local",
      command: [process.execPath, path.join(rootDir, "src", "shared", "judge_mcp.mjs")],
      environment: {
        INSTANCE_ID: "{env:HIL_DYNAMICS_INSTANCE_ID}",
        HARNESS_HUMAN_KB: "{env:HARNESS_HUMAN_KB}",
        HARNESS_ASK_HUMAN_CACHE: "{env:HARNESS_ASK_HUMAN_CACHE}",
        HARNESS_ASK_HUMAN_REPLAY: "{env:HARNESS_ASK_HUMAN_REPLAY}",
        HARNESS_TRAJECTORY_FILE: "{env:HARNESS_TRAJECTORY_FILE}",
        HARNESS_WORKSPACE_DIR: "{env:HARNESS_WORKSPACE_DIR}",
        HARNESS_APPROVAL_POLICY_ROUTER: "{env:HARNESS_APPROVAL_POLICY_ROUTER}",
        ASK_HUMAN_MODEL: "{env:ASK_HUMAN_MODEL}",
        ASK_HUMAN_SEED: "{env:ASK_HUMAN_SEED}",
        LITELLM_BASE_URL: "{env:LITELLM_BASE_URL}",
        LITELLM_API_KEY: "{env:LITELLM_API_KEY}",
      },
      enabled: true,
      timeout: 60000,
    },
  };
}

async function runAttempt({ row, attemptIndex, args, runDir }) {
  const instanceId = row.instance_id;
  const attemptDir = path.join(runDir, "trajectories", harness.name, instanceId, `attempt-${attemptIndex}`);
  const trajectoryFile = path.join(attemptDir, "trajectory.jsonl");
  const workspaceDir = attemptWorkspace(attemptDir);
  const predictionPrefix = `${args.runId}__${harness.name}__${instanceId}__attempt-${attemptIndex}`;
  const predictionPath = path.join(attemptDir, "prediction.json");

  if (args.resume && (await pathExists(predictionPath))) {
    const fs = await import("node:fs/promises");
    const prediction = JSON.parse(await fs.readFile(predictionPath, "utf8"));
    await appendJsonl(trajectoryFile, { type: "attempt_resume_skip", timestamp: new Date().toISOString(), prediction_path: predictionPath });
    return prediction;
  }

  const archivedTo = await archiveExistingAttempt({ runDir, attemptDir, harnessName: harness.name, instanceId, attemptIndex });
  await ensureDir(attemptDir);
  if (archivedTo) {
    await appendJsonl(trajectoryFile, {
      type: "attempt_archive_previous",
      timestamp: new Date().toISOString(),
      archived_to: archivedTo,
      reason: args.resume ? "resume_incomplete_attempt" : "fresh_rerun",
    });
  }
  const prompt = `${promptForInstance(row, {
    harnessName: harness.name,
    mode: args.mode,
    clarificationInstructionProfile: args.clarificationInstructionProfile,
  })}\nUse the available file editing and shell tools to make the fix. Work only inside this checkout.\n`;
  await writeText(path.join(attemptDir, "prompt.md"), prompt);
  await writeJson(path.join(attemptDir, "attempt.json"), {
    run_id: args.runId,
    harness: harness.name,
    mode: args.mode,
    instance_id: instanceId,
    attempt_index: attemptIndex,
    prefix: predictionPrefix,
    model: args.model,
    max_steps: args.maxSteps,
    attempt_timeout_ms: args.attemptTimeoutMs,
    human_input_enabled: Boolean(args.humanKb),
    ask_human_cache_configured: Boolean(args.askHumanCache),
    ask_human_replay: args.askHumanReplay,
    ask_human_model: args.askHumanModel,
    clarification_instruction_profile: args.clarificationInstructionProfile,
    approval_policy_router: args.approvalPolicyRouter,
    metadata_shown_to_agent: publicMetadata(row),
    started_at: new Date().toISOString(),
  });
  await appendJsonl(trajectoryFile, { type: "attempt_start", timestamp: new Date().toISOString(), instance_id: instanceId, attempt_index: attemptIndex, prompt });

  await cloneCheckout({ row, workspaceDir, trajectoryFile });

  const attemptHome = path.join(attemptDir, ".home");
  const xdgDataHome = path.join(attemptDir, ".local", "share");
  const xdgConfigHome = path.join(attemptDir, ".config");
  await ensureDir(attemptHome);
  await ensureDir(xdgDataHome);
  await ensureDir(xdgConfigHome);

  let sdkError = null;
  let port = null;
  let server = null;
  const attemptTimeoutMs = Number(args.attemptTimeoutMs || 0);
  const abortController = attemptTimeoutMs > 0 ? new AbortController() : new AbortController();
  const timeoutId = attemptTimeoutMs > 0
    ? setTimeout(() => abortController.abort(new Error(`OpenCode attempt timed out after ${attemptTimeoutMs}ms`)), attemptTimeoutMs)
    : null;
  try {
    port = await acquirePort({ owner: `${args.runId}:${harness.name}:${instanceId}:attempt-${attemptIndex}` });
    const env = await opencodeEnv({
      HOME: attemptHome,
      XDG_DATA_HOME: xdgDataHome,
      XDG_CONFIG_HOME: xdgConfigHome,
      PATH: `${path.join(rootDir, "node_modules", ".bin")}${path.delimiter}${process.env.PATH || ""}`,
      ...mcpRuntimeEnv({ instanceId, args, trajectoryFile, workspaceDir }),
    });
    const humanInstruction = args.mode === "ask_human"
      ? `${clarificationInstruction({
          harnessName: harness.name,
          profile: args.clarificationInstructionProfile,
          system: true,
        })} `
      : "";
    const agentPrompt = `You are running inside an automated software engineering harness. ${humanInstruction}Work only inside the attempt workspace.`;
    const config = await opencodeConfig({
      model: args.model,
      mcp: mcpConfig({ args }),
      agentPrompt,
    });
    const created = await withTemporaryProcessEnv(env, async () =>
      createOpencode({
        hostname: "127.0.0.1",
        port,
        signal: abortController.signal,
        timeout: Number(process.env.OPENCODE_SERVER_START_TIMEOUT_MS || 15000),
        config,
      })
    );
    server = created.server;
    const client = created.client;
    await appendJsonl(trajectoryFile, { type: "opencode_server_start", timestamp: new Date().toISOString(), url: server.url, port });
    const session = unwrap(await client.session.create({ body: { title: instanceId }, query: { directory: workspaceDir } }), "OpenCode session.create");
    const sessionId = session?.id || session?.info?.id;
    if (!sessionId) throw new Error(`OpenCode did not return a session id: ${JSON.stringify(session)}`);
    const humanRouter = createHumanInputRouter({
      instanceId,
      kbPath: args.humanKb,
      cachePath: args.askHumanCache,
      replay: args.askHumanReplay,
      modelId: args.askHumanModel,
      seed: args.askHumanSeed,
      trajectoryFile,
      workspaceDir,
      approvalPolicy: args.approvalPolicyRouter,
    });
    const eventConsumer = consumeEvents({ client, sessionId, trajectoryFile, router: humanRouter, abortSignal: abortController.signal });
    const { providerID, modelID } = providerModel(args.model);
    const promptRequest = {
      path: { id: sessionId },
      query: { directory: workspaceDir },
      signal: abortController.signal,
      body: {
        model: { providerID, modelID },
        agent: "build",
        parts: [{ type: "text", text: prompt }],
      },
    };
    try {
      if (typeof client.session.promptAsync === "function") {
        unwrap(await client.session.promptAsync(promptRequest), "OpenCode session.promptAsync");
        await waitForOpenCodeIdle({ client, sessionId, workspaceDir, trajectoryFile, abortSignal: abortController.signal });
      } else {
        await client.session.prompt(promptRequest);
      }
      await flushSessionMessages({ client, sessionId, trajectoryFile });
    } finally {
      abortController.abort(new Error("OpenCode prompt completed"));
      await eventConsumer.catch((error) => {
        throw error;
      });
    }
  } catch (error) {
    const text = redactString(String(error?.stack || error));
    sdkError = abortController?.signal.aborted && attemptTimeoutMs > 0 && /timed out/i.test(String(abortController.signal.reason || ""))
      ? `OpenCode attempt timed out after ${attemptTimeoutMs}ms.\n\n${text}`
      : text;
    await appendJsonl(trajectoryFile, { type: "sdk_error", timestamp: new Date().toISOString(), error: sdkError });
  } finally {
    if (timeoutId) clearTimeout(timeoutId);
    if (server) server.close();
    await cleanupOpenCodeServer({ port, trajectoryFile }).catch(async (error) => {
      await appendJsonl(trajectoryFile, { type: "opencode_server_cleanup_error", timestamp: new Date().toISOString(), error: String(error?.stack || error) });
    });
    await new Promise((resolve) => setTimeout(resolve, 250));
    if (port !== null) await releasePort(port);
  }

  const patch = await diff(workspaceDir, trajectoryFile);
  await writeText(path.join(attemptDir, "patch.diff"), patch);
  const prediction = {
    instance_id: instanceId,
    patch,
    prefix: predictionPrefix,
    harness: harness.name,
    mode: args.mode,
    model: args.model,
    attempt_index: attemptIndex,
    run_id: args.runId,
    sdk_error: sdkError,
  };
  await writeJsonAtomic(predictionPath, prediction);
  await appendJsonl(trajectoryFile, {
    type: "submission",
    timestamp: new Date().toISOString(),
    prediction_path: predictionPath,
    patch_path: path.join(attemptDir, "patch.diff"),
    prefix: predictionPrefix,
    patch_bytes: Buffer.byteLength(patch),
    sdk_error: sdkError,
  });
  await appendJsonl(trajectoryFile, { type: "attempt_end", timestamp: new Date().toISOString(), patch_bytes: Buffer.byteLength(patch), sdk_error: sdkError });
  return prediction;
}
