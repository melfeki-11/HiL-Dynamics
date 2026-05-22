import { spawn } from "node:child_process";
import path from "node:path";
import { rootDir } from "../../shared/config.mjs";
import { clarificationInstruction } from "../../shared/dataset.mjs";
import { createHumanInputRouter, UNKNOWN_RESOLUTION } from "../../shared/human_input.mjs";
import { appendJsonl } from "../../shared/io.mjs";

class JsonRpcProcess {
  constructor({ command, args, cwd, env, trajectoryFile, onRequest, onNotification }) {
    this.nextId = 1;
    this.pending = new Map();
    this.buffer = "";
    this.trajectoryFile = trajectoryFile;
    this.onRequest = onRequest;
    this.onNotification = onNotification;
    this.child = spawn(command, args, { cwd, env, stdio: ["pipe", "pipe", "pipe"] });
    this.child.stdout.on("data", (chunk) => this.onStdout(chunk));
    this.child.stderr.on("data", async (chunk) => {
      await appendJsonl(this.trajectoryFile, { type: "codex_app_server_stderr", timestamp: new Date().toISOString(), text: chunk.toString() });
    });
    this.child.on("exit", (code, signal) => {
      const error = new Error(`codex app-server exited code=${code} signal=${signal}`);
      for (const { reject } of this.pending.values()) reject(error);
      this.pending.clear();
    });
  }

  async onStdout(chunk) {
    this.buffer += chunk.toString();
    while (true) {
      const index = this.buffer.indexOf("\n");
      if (index < 0) return;
      const line = this.buffer.slice(0, index).trim();
      this.buffer = this.buffer.slice(index + 1);
      if (!line) continue;
      let message;
      try {
        message = JSON.parse(line);
      } catch {
        await appendJsonl(this.trajectoryFile, { type: "codex_app_server_stdout", timestamp: new Date().toISOString(), text: line });
        continue;
      }
      await this.handleMessage(message);
    }
  }

  async handleMessage(message) {
    if (message.id !== undefined && message.method) {
      await appendJsonl(this.trajectoryFile, { type: "codex_app_server_request", timestamp: new Date().toISOString(), request: message });
      try {
        const result = await this.onRequest(message);
        await appendJsonl(this.trajectoryFile, {
          type: "codex_app_server_response",
          timestamp: new Date().toISOString(),
          request_id: message.id,
          request_method: message.method,
          response: result,
        });
        this.write({ jsonrpc: "2.0", id: message.id, result });
      } catch (error) {
        const responseError = { code: -32000, message: String(error?.message || error) };
        await appendJsonl(this.trajectoryFile, {
          type: "codex_app_server_response",
          timestamp: new Date().toISOString(),
          request_id: message.id,
          request_method: message.method,
          error: responseError,
        });
        this.write({
          jsonrpc: "2.0",
          id: message.id,
          error: responseError,
        });
      }
      return;
    }
    if (message.id !== undefined) {
      const pending = this.pending.get(message.id);
      if (!pending) return;
      this.pending.delete(message.id);
      if (message.error) pending.reject(new Error(JSON.stringify(message.error)));
      else pending.resolve(message.result);
      return;
    }
    if (message.method) {
      await appendJsonl(this.trajectoryFile, { type: "sdk_event", timestamp: new Date().toISOString(), event: message });
      await this.onNotification?.(message);
    }
  }

  request(method, params = {}) {
    const id = this.nextId;
    this.nextId += 1;
    const message = { jsonrpc: "2.0", id, method, params };
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.write(message);
    });
  }

  write(message) {
    this.child.stdin.write(`${JSON.stringify(message)}\n`);
  }

  close() {
    this.child.kill("SIGTERM");
  }
}

export async function runCodexAppServerAttempt({ prompt, args, env, workspaceDir, attemptDir, trajectoryFile, instanceId, abortSignal }) {
  const router = createHumanInputRouter({
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

  let threadId = null;
  let turnDoneResolve;
  let turnDoneReject;
  const turnDone = new Promise((resolve, reject) => {
    turnDoneResolve = resolve;
    turnDoneReject = reject;
  });
  const codexBin = path.join(rootDir, "node_modules", ".bin", "codex");
  const rpc = new JsonRpcProcess({
    command: codexBin,
    args: ["app-server", "--enable", "default_mode_request_user_input", "--listen", "stdio://"],
    cwd: rootDir,
    env,
    trajectoryFile,
    onRequest: (message) => handleCodexServerRequest({ message, router, workspaceDir, instanceId }),
    onNotification: (message) => {
      if (message.method === "turn/completed" && (!threadId || message.params?.threadId === threadId)) turnDoneResolve(message.params);
      if (message.method === "error" && !message.params?.willRetry) turnDoneReject(new Error(JSON.stringify(message.params)));
    },
  });
  const abortHandler = () => {
    rpc.close();
    turnDoneReject(new Error("Codex app-server attempt aborted"));
  };
  abortSignal?.addEventListener("abort", abortHandler, { once: true });
  try {
    await appendJsonl(trajectoryFile, { type: "codex_app_server_start", timestamp: new Date().toISOString(), attempt_dir: attemptDir });
    await rpc.request("initialize", {
      clientInfo: { name: "hil-dynamics", title: "HiL-Dynamics", version: "0.1.0" },
      capabilities: { experimentalApi: true },
    });
    const threadStart = await rpc.request("thread/start", {
      cwd: workspaceDir,
      model: args.model,
      modelProvider: "litellm",
      approvalPolicy: args.codexApprovalPolicy || "on-request",
      approvalsReviewer: "user",
      sandbox: "workspace-write",
      config: env.CODEX_APP_CONFIG ? JSON.parse(env.CODEX_APP_CONFIG) : undefined,
      developerInstructions: `You are running inside an automated software engineering harness. ${clarificationInstruction({
        harnessName: "codex",
        profile: args.clarificationInstructionProfile,
        system: true,
      })} Work only inside the attempt workspace.`,
      ephemeral: true,
    });
    threadId = threadStart?.thread?.id;
    if (!threadId) throw new Error(`Codex app-server did not return a thread id: ${JSON.stringify(threadStart)}`);
    await rpc.request("turn/start", {
      threadId,
      input: [{ type: "text", text: prompt, text_elements: [] }],
      cwd: workspaceDir,
      approvalPolicy: args.codexApprovalPolicy || "on-request",
      approvalsReviewer: "user",
      sandboxPolicy: {
        type: "workspaceWrite",
        writableRoots: [workspaceDir],
        networkAccess: true,
        excludeTmpdirEnvVar: false,
        excludeSlashTmp: false,
      },
      model: args.model,
      effort: args.modelReasoningEffort,
    });
    return await turnDone;
  } finally {
    abortSignal?.removeEventListener("abort", abortHandler);
    rpc.close();
  }
}

async function handleCodexServerRequest({ message, router, workspaceDir, instanceId }) {
  const { method, params } = message;
  if (method === "item/tool/requestUserInput") return handleRequestUserInput({ method, params, router });
  if (method === "mcpServer/elicitation/request") return handleElicitation({ method, params, router });
  if (method === "item/commandExecution/requestApproval") return handleCommandApproval({ method, params, router });
  if (method === "item/fileChange/requestApproval") return handleFileChangeApproval({ method, params, router });
  if (method === "item/permissions/requestApproval") return handlePermissionsApproval({ method, params, router });
  if (method === "execCommandApproval") return handleLegacyExecApproval({ method, params, router });
  if (method === "applyPatchApproval") return handleLegacyPatchApproval({ method, params, router, workspaceDir });
  if (method === "item/tool/call") {
    return { error: `Unsupported dynamic tool call in harness for instance ${instanceId}` };
  }
  return {};
}

async function handleRequestUserInput({ method, params, router }) {
  const answers = {};
  for (const question of params.questions || []) {
    const result = await router.route({
      requestType: "clarification",
      nativeEventType: `codex.${method}`,
      rawEvent: question,
      question: `${question.header ? `${question.header}: ` : ""}${question.question}`,
      options: question.options || [],
      context: { question_id: question.id, isOther: question.isOther, isSecret: question.isSecret },
    });
    const selected = result.selected_labels?.length ? result.selected_labels : [result.resolution || UNKNOWN_RESOLUTION];
    answers[question.id] = { answers: selected };
  }
  return { answers };
}

async function handleElicitation({ method, params, router }) {
  const result = await router.route({
    requestType: "elicitation",
    nativeEventType: `codex.${method}`,
    rawEvent: params,
    question: params.message || params.url || "MCP elicitation request",
    context: { serverName: params.serverName, mode: params.mode, requestedSchema: params.requestedSchema },
  });
  if (result.status !== "answered") return { action: "decline", content: null, _meta: null };
  return { action: "accept", content: parseResolutionJson(result.resolution), _meta: null };
}

async function handleCommandApproval({ method, params, router }) {
  const routed = await router.routeApproval({
    nativeEventType: `codex.${method}`,
    rawEvent: params,
    question: params.reason || `Approve command execution: ${params.command || ""}`,
    context: { command: params.command, cwd: params.cwd, commandActions: params.commandActions },
  });
  return { decision: routed.approval.allowed ? "accept" : "decline" };
}

async function handleFileChangeApproval({ method, params, router }) {
  const routed = await router.routeApproval({
    nativeEventType: `codex.${method}`,
    rawEvent: params,
    question: params.reason || `Approve file change request${params.grantRoot ? ` under ${params.grantRoot}` : ""}`,
    context: { grantRoot: params.grantRoot },
  });
  return { decision: routed.approval.allowed ? "accept" : "decline" };
}

async function handlePermissionsApproval({ method, params, router }) {
  const routed = await router.routeApproval({
    requestType: "permission",
    nativeEventType: `codex.${method}`,
    rawEvent: params,
    question: params.reason || "Approve requested permissions",
    context: { cwd: params.cwd, permissions: params.permissions },
  });
  const permissions = routed.approval.allowed
    ? {
        ...(params.permissions?.network ? { network: params.permissions.network } : {}),
        ...(params.permissions?.fileSystem ? { fileSystem: params.permissions.fileSystem } : {}),
      }
    : {};
  return { permissions, scope: "turn", strictAutoReview: false };
}

async function handleLegacyExecApproval({ method, params, router }) {
  const routed = await router.routeApproval({
    nativeEventType: `codex.${method}`,
    rawEvent: params,
    question: params.reason || `Approve command execution: ${(params.command || []).join(" ")}`,
    context: { command: params.command, cwd: params.cwd, parsedCmd: params.parsedCmd },
  });
  return { decision: routed.approval.allowed ? "approved" : "denied" };
}

async function handleLegacyPatchApproval({ method, params, router, workspaceDir }) {
  const routed = await router.routeApproval({
    nativeEventType: `codex.${method}`,
    rawEvent: params,
    question: params.reason || "Approve patch application",
    context: { grantRoot: params.grantRoot, paths: Object.keys(params.fileChanges || {}).map((file) => path.join(workspaceDir, file)) },
  });
  return { decision: routed.approval.allowed ? "approved" : "denied" };
}

function parseResolutionJson(resolution) {
  try {
    return JSON.parse(resolution);
  } catch {
    return { answer: resolution };
  }
}
