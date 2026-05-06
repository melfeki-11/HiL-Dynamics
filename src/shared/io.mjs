import fs from "node:fs/promises";
import path from "node:path";
import { redactString } from "./redact.mjs";

const appendLocks = new Map();
const FILE_LOCK_RETRY_MS = Number(process.env.TRAJECTORY_JSONL_LOCK_RETRY_MS || 25);
const FILE_LOCK_TIMEOUT_MS = Number(process.env.TRAJECTORY_JSONL_LOCK_TIMEOUT_MS || 120000);
const FILE_LOCK_STALE_MS = Number(process.env.TRAJECTORY_JSONL_LOCK_STALE_MS || 600000);

export async function ensureDir(dir) {
  await fs.mkdir(dir, { recursive: true });
}

export async function pathExists(filePath) {
  try {
    await fs.access(filePath);
    return true;
  } catch {
    return false;
  }
}

export async function readJsonl(filePath) {
  const text = await fs.readFile(filePath, "utf8");
  return text.split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line));
}

export async function writeJson(filePath, value) {
  await ensureDir(path.dirname(filePath));
  await fs.writeFile(filePath, JSON.stringify(value, null, 2) + "\n", "utf8");
}

export async function writeJsonAtomic(filePath, value) {
  await ensureDir(path.dirname(filePath));
  const tempPath = path.join(path.dirname(filePath), `.${path.basename(filePath)}.${process.pid}.${Date.now()}.${Math.random().toString(16).slice(2)}.tmp`);
  await fs.writeFile(tempPath, JSON.stringify(value, null, 2) + "\n", "utf8");
  await fs.rename(tempPath, filePath);
}

export async function writeText(filePath, text) {
  await ensureDir(path.dirname(filePath));
  await fs.writeFile(filePath, text, "utf8");
}

export async function appendJsonl(filePath, value) {
  const previous = appendLocks.get(filePath) || Promise.resolve();
  const current = previous.then(async () => {
    await ensureDir(path.dirname(filePath));
    const redacted = redactValue(value);
    if (path.basename(filePath) === "trajectory.jsonl") {
      await appendTrajectoryEvent(filePath, redacted);
      return;
    }
    await fs.appendFile(filePath, JSON.stringify(redacted) + "\n", "utf8");
  });
  const stored = current.catch(() => {});
  appendLocks.set(filePath, stored);
  try {
    await current;
  } finally {
    if (appendLocks.get(filePath) === stored) appendLocks.delete(filePath);
  }
}

function redactValue(value) {
  if (typeof value === "string") return redactString(value);
  if (Array.isArray(value)) return value.map(redactValue);
  if (value && typeof value === "object") {
    const out = {};
    for (const [key, item] of Object.entries(value)) {
      if (/^is[_-]?secret$/i.test(key)) out[key] = item;
      else if (/(token|secret|api[_-]?key|auth|hil_bench)/i.test(key)) out[key] = "[REDACTED]";
      else out[key] = redactValue(item);
    }
    return out;
  }
  return value;
}

async function appendTrajectoryEvent(filePath, event) {
  const lockPath = `${filePath}.lock`;
  await withFileLock(lockPath, async () => {
    const normalized = await maybeNormalizeTraceEventLocked(filePath, event);
    await fs.appendFile(filePath, JSON.stringify(normalized) + "\n", "utf8");
  });
}

async function maybeNormalizeTraceEventLocked(filePath, event) {
  const traceMeta = inferTraceMeta(filePath);
  if (!traceMeta) return event;
  const eventIndex = await countJsonlLines(filePath);
  return normalizeTraceEvent({ event, eventIndex, traceMeta });
}

async function countJsonlLines(filePath) {
  try {
    const text = await fs.readFile(filePath, "utf8");
    return text.split(/\r?\n/).filter(Boolean).length;
  } catch (error) {
    if (error?.code === "ENOENT") return 0;
    throw error;
  }
}

async function withFileLock(lockPath, fn) {
  const release = await acquireFileLock(lockPath);
  try {
    return await fn();
  } finally {
    await release();
  }
}

async function acquireFileLock(lockPath) {
  const startedAt = Date.now();
  let staleRemoved = false;
  while (true) {
    let handle = null;
    try {
      handle = await fs.open(lockPath, "wx");
      await handle.writeFile(JSON.stringify({ pid: process.pid, created_at: new Date().toISOString() }) + "\n", "utf8");
      return async () => {
        try {
          await handle.close();
        } finally {
          try {
            await fs.unlink(lockPath);
          } catch (error) {
            if (error?.code !== "ENOENT") throw error;
          }
        }
      };
    } catch (error) {
      if (handle) {
        try {
          await handle.close();
        } catch {}
      }
      if (error?.code !== "EEXIST") throw error;
      const ageMs = await lockAgeMs(lockPath);
      if (ageMs !== null && ageMs > FILE_LOCK_STALE_MS) {
        try {
          await fs.unlink(lockPath);
          staleRemoved = true;
          continue;
        } catch (unlinkError) {
          if (unlinkError?.code !== "ENOENT") throw unlinkError;
        }
      }
      if (Date.now() - startedAt > FILE_LOCK_TIMEOUT_MS) {
        const staleNote = staleRemoved ? " A stale lock was removed during acquisition." : "";
        throw new Error(`Timed out waiting for JSONL lock ${lockPath}.${staleNote}`);
      }
      await sleep(FILE_LOCK_RETRY_MS);
    }
  }
}

async function lockAgeMs(lockPath) {
  try {
    const stat = await fs.stat(lockPath);
    return Date.now() - stat.mtimeMs;
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw error;
  }
}

async function sleep(ms) {
  await new Promise((resolve) => setTimeout(resolve, ms));
}

function inferTraceMeta(filePath) {
  const parts = path.resolve(filePath).split(path.sep);
  const attempt = parts.at(-2) || "";
  const instanceId = parts.at(-3);
  const harness = parts.at(-4);
  const trajectories = parts.at(-5);
  const runId = parts.at(-6);
  if (trajectories !== "trajectories" || !runId || !harness || !instanceId || !/^attempt-\d+$/.test(attempt)) return null;
  return {
    run_id: runId,
    harness,
    instance_id: instanceId,
    attempt_index: Number(attempt.slice("attempt-".length)),
  };
}

function normalizeTraceEvent({ event, eventIndex, traceMeta }) {
  const timestamp = event.timestamp || event.started_at || event.ended_at || new Date().toISOString();
  const normalized = {
    ...event,
    run_id: traceMeta.run_id,
    instance_id: event.instance_id || traceMeta.instance_id,
    harness: traceMeta.harness,
    attempt_index: event.attempt_index || traceMeta.attempt_index,
    event_index: eventIndex,
    timestamp,
    event_type: "unknown",
    native_event_type: event.native_event_type || null,
    native_payload: null,
    normalized_request_type: null,
    content: null,
    tool_name: null,
    tool_args: null,
    observation: null,
    question: null,
    answer: null,
    ask_human_status: "not_applicable",
    matched_blocker_ids: [],
    matched_source_ids: [],
    approval_decision: "not_applicable",
    approval_grounding: "not_applicable",
    files_changed: [],
    commands_run: [],
    tests_run: [],
    patch_path: null,
    final_status: "unknown",
    audit: {},
  };

  const type = String(event.type || "");
  if (type === "attempt_start") {
    normalized.event_type = "tool_result";
    normalized.content = event.prompt || null;
  } else if (type === "command_start" || type === "command_end") {
    applyCommandEvent(normalized, event);
  } else if (type === "sdk_message") {
    applyClaudeSdkMessage(normalized, event.message);
  } else if (type === "sdk_event") {
    applyCodexSdkEvent(normalized, event.event);
  } else if (type === "opencode_event") {
    applyOpenCodeEvent(normalized, event.event);
  } else if (type === "opencode_permission_response") {
    normalized.event_type = "permission_result";
    normalized.native_event_type = "opencode.permission.response";
    normalized.native_payload = event;
    normalized.normalized_request_type = "permission";
    normalized.approval_decision = event.response === "reject" ? "denied" : "approved";
    normalized.tool_args = { permission_id: event.permission_id, session_id: event.session_id };
  } else if (type === "opencode_server_start") {
    normalized.event_type = "tool_result";
    normalized.native_event_type = "opencode.server.start";
    normalized.native_payload = event;
  } else if (type === "opencode_server_cleanup") {
    normalized.event_type = "tool_result";
    normalized.native_event_type = "opencode.server.cleanup";
    normalized.native_payload = event;
  } else if (type === "opencode_server_cleanup_error") {
    normalized.event_type = "error";
    normalized.native_event_type = "opencode.server.cleanup_error";
    normalized.native_payload = event;
    normalized.content = event.error || null;
  } else if (type === "opencode_session_status") {
    normalized.event_type = "tool_result";
    normalized.native_event_type = "opencode.session.status";
    normalized.native_payload = event;
    normalized.content = event.status ? JSON.stringify(event.status) : null;
  } else if (type === "codex_app_server_request") {
    applyCodexAppServerRequest(normalized, event.request);
  } else if (type === "codex_app_server_response") {
    normalized.event_type = "tool_result";
    normalized.native_event_type = event.request_method ? `codex.${event.request_method}` : "codex.response";
    normalized.native_payload = event.response || event.error || null;
    normalized.content = event.request_method || null;
    normalized.tool_args = { request_id: event.request_id ?? null };
  } else if (type === "human_input_raw_event") {
    applyHumanInputRequest(normalized, event, event.raw_event || null);
  } else if (type === "human_input_normalized_event") {
    applyHumanInputRequest(normalized, event, event.request || null);
  } else if (type === "human_input_result") {
    applyAskHumanResult(normalized, event);
  } else if (type === "human_input_approval_decision") {
    applyApprovalResult(normalized, event);
  } else if (type === "submission") {
    normalized.event_type = "patch_submit";
    normalized.patch_path = event.patch_path || null;
    normalized.content = event.prefix || null;
    normalized.final_status = event.final_status || (event.sdk_error || event.generation_failed ? "error" : "unknown");
  } else if (type === "smoke_hidden_test_result") {
    normalized.event_type = "final";
    normalized.content = event.command || null;
    normalized.observation = [event.stdout, event.stderr].filter(Boolean).join("\n") || null;
    normalized.tests_run = [
      {
        command: event.command || "python3 -m pytest -q",
        cwd: event.cwd || null,
        code: event.code,
      },
    ];
    normalized.final_status = event.passed ? "pass" : "fail";
  } else if (type === "attempt_end") {
    normalized.event_type = "final";
    normalized.final_status = event.final_status || (event.sdk_error || event.generation_failed ? "error" : "unknown");
  } else if (type === "sdk_error" || type === "attempt_error") {
    normalized.event_type = "error";
    normalized.content = event.error || null;
    normalized.final_status = "error";
  } else if (type === "workspace_symlink" || type === "workspace_reuse" || type === "workspace_quarantine") {
    normalized.event_type = "tool_result";
    normalized.native_payload = event;
  }

  return normalized;
}

function applyCommandEvent(normalized, event) {
  const command = [event.command, ...(Array.isArray(event.args) ? event.args : [])].filter(Boolean).join(" ");
  const commandRecord = {
    command: event.command,
    args: Array.isArray(event.args) ? event.args : [],
    cwd: event.cwd || null,
    code: event.code,
    signal: event.signal,
    started_at: event.started_at,
    ended_at: event.ended_at,
  };
  normalized.event_type = isTestCommand(command) ? "test" : "command";
  normalized.content = command || null;
  normalized.commands_run = command ? [commandRecord] : [];
  normalized.tests_run = isTestCommand(command) ? [commandRecord] : [];
  normalized.observation = [event.stdout, event.stderr].filter(Boolean).join("\n") || null;
  normalized.native_payload = event;
}

function applyClaudeSdkMessage(normalized, message) {
  normalized.native_event_type = message?.type ? `claude.${message.type}` : "claude.sdk_message";
  normalized.native_payload = message || null;
  normalized.content = extractText(message);
  const toolUse = findClaudeContent(message, "tool_use");
  const toolResult = findClaudeContent(message, "tool_result");
  if (toolUse) {
    normalized.event_type = "tool_call";
    normalized.tool_name = toolUse.name || null;
    normalized.tool_args = toolUse.input || null;
  } else if (toolResult) {
    normalized.event_type = "tool_result";
    normalized.tool_name = toolResult.tool_use_id || null;
    normalized.observation = extractText(toolResult);
  } else if (message?.type === "result") {
    normalized.event_type = "final";
    normalized.final_status = message.subtype === "success" ? "unknown" : "error";
  } else {
    normalized.event_type = "tool_result";
  }
}

function applyCodexSdkEvent(normalized, event) {
  normalized.native_event_type = event?.method ? `codex.${event.method}` : event?.type || "codex.sdk_event";
  normalized.native_payload = event || null;
  if (event?.method) {
    applyCodexAppServerNotification(normalized, event);
    return;
  }
  const item = event?.item;
  if (!item) {
    normalized.event_type = event?.type === "turn.completed" ? "final" : "tool_result";
    return;
  }
  if (item.type === "command_execution") {
    const commandRecord = { command: item.command, code: item.exit_code, status: item.status };
    normalized.event_type = isTestCommand(item.command) ? "test" : "command";
    normalized.content = item.command || null;
    normalized.commands_run = item.command ? [commandRecord] : [];
    normalized.tests_run = isTestCommand(item.command) ? [commandRecord] : [];
    normalized.observation = item.aggregated_output || null;
  } else if (item.type === "file_change") {
    normalized.event_type = "file_edit";
    normalized.files_changed = Array.isArray(item.changes) ? item.changes.map((change) => change.path).filter(Boolean) : [];
  } else if (item.type === "mcp_tool_call") {
    normalized.event_type = item.status === "completed" || item.status === "failed" ? "tool_result" : "tool_call";
    normalized.tool_name = `${item.server || ""}.${item.tool || ""}`.replace(/^\./, "");
    normalized.tool_args = item.arguments ?? null;
    normalized.observation = item.result || item.error || null;
  } else if (item.type === "agent_message" || item.type === "reasoning" || item.type === "error") {
    normalized.event_type = item.type === "error" ? "error" : "tool_result";
    normalized.content = item.text || item.message || null;
  }
}

function applyCodexAppServerRequest(normalized, request) {
  const method = request?.method || "request";
  const requestType = requestTypeForNativeEvent(method);
  normalized.event_type = requestEventType(requestType);
  normalized.native_event_type = `codex.${method}`;
  normalized.native_payload = request || null;
  normalized.normalized_request_type = requestType;
  normalized.content = method;
  normalized.question = questionForCodexServerRequest(request) || null;
  normalized.tool_args = request
    ? {
        request_id: request.id ?? null,
        params: request.params ?? null,
      }
    : null;
}

function applyCodexAppServerNotification(normalized, event) {
  const method = event.method || "";
  normalized.content = method;
  const params = event.params || {};
  const item = params.item;

  if (method === "turn/completed") {
    normalized.event_type = "final";
    normalized.final_status = "unknown";
    return;
  }
  if (method === "error" || method === "turn/error") {
    normalized.event_type = "error";
    normalized.content = params.message || params.error || method;
    normalized.final_status = "error";
    return;
  }
  if (method === "turn/diff/updated") {
    const diff = String(params.diff || "");
    normalized.event_type = "file_edit";
    normalized.observation = diff || null;
    normalized.files_changed = filesFromDiff(diff);
    return;
  }
  if (method === "item/commandExecution/outputDelta" || method === "command/exec/outputDelta") {
    normalized.event_type = "tool_result";
    normalized.observation = params.delta || null;
    return;
  }
  if (method === "item/fileChange/outputDelta" || method === "item/fileChange/patchUpdated") {
    const text = String(params.delta || params.diff || "");
    normalized.event_type = "file_edit";
    normalized.observation = text || null;
    normalized.files_changed = filesFromDiff(text);
    return;
  }
  if (!item) {
    normalized.event_type = "tool_result";
    normalized.content = params.message || params.text || method;
    return;
  }

  applyCodexItem(normalized, item);
}

function applyCodexItem(normalized, item) {
  const itemType = String(item.type || "");
  const canonicalType = itemType.replace(/[A-Z]/g, (char) => `_${char.toLowerCase()}`);
  if (canonicalType === "command_execution") {
    const command = item.command || null;
    const code = item.exit_code ?? item.exitCode;
    const commandRecord = {
      command,
      cwd: item.cwd || null,
      code,
      status: item.status || null,
      process_id: item.processId || item.process_id || null,
      source: item.source || null,
      duration_ms: item.durationMs || item.duration_ms || null,
      command_actions: item.commandActions || item.command_actions || null,
    };
    normalized.event_type = isTestCommand(command) ? "test" : "command";
    normalized.content = command;
    normalized.commands_run = command ? [commandRecord] : [];
    normalized.tests_run = isTestCommand(command) ? [commandRecord] : [];
    normalized.observation = item.aggregatedOutput ?? item.aggregated_output ?? null;
  } else if (canonicalType === "file_change") {
    normalized.event_type = "file_edit";
    normalized.files_changed = Array.isArray(item.changes) ? item.changes.map((change) => change.path).filter(Boolean) : [];
    normalized.observation = item.error || null;
  } else if (canonicalType === "mcp_tool_call") {
    normalized.event_type = item.status === "completed" || item.status === "failed" ? "tool_result" : "tool_call";
    normalized.tool_name = `${item.server || ""}.${item.tool || item.toolName || ""}`.replace(/^\./, "");
    normalized.tool_args = item.arguments ?? null;
    normalized.observation = item.result || item.error || null;
  } else if (canonicalType === "agent_message" || canonicalType === "reasoning" || canonicalType === "user_message") {
    normalized.event_type = "tool_result";
    normalized.content = item.text || extractText(item) || null;
  } else if (canonicalType === "error") {
    normalized.event_type = "error";
    normalized.content = item.message || item.text || null;
  } else {
    normalized.event_type = "tool_result";
    normalized.content = item.text || item.message || itemType || null;
  }
}

function applyOpenCodeEvent(normalized, event) {
  const payload = event?.payload || event;
  const eventType = payload?.type || event?.type || "opencode.event";
  normalized.native_event_type = `opencode.${eventType}`;
  normalized.native_payload = payload || event || null;
  normalized.content = eventType;
  const properties = payload?.properties || {};
  if (eventType === "message.part.updated") {
    applyOpenCodePart(normalized, properties.part || {});
  } else if (eventType === "permission.updated") {
    const permission = properties;
    normalized.event_type = "permission_request";
    normalized.normalized_request_type = "permission";
    normalized.question = permission.title || `Approve OpenCode permission ${permission.type || ""}`;
    normalized.tool_name = permission.type || null;
    normalized.tool_args = {
      permission_id: permission.id || null,
      pattern: permission.pattern || null,
      metadata: permission.metadata || {},
    };
  } else if (eventType === "permission.replied") {
    normalized.event_type = "permission_result";
    normalized.normalized_request_type = "permission";
    normalized.approval_decision = properties.response === "reject" ? "denied" : "approved";
    normalized.tool_args = { permission_id: properties.permissionID || null, session_id: properties.sessionID || null };
  } else if (eventType === "command.executed") {
    const command = properties.arguments ? `${properties.name || ""} ${properties.arguments}`.trim() : properties.name;
    const commandRecord = { command, code: null, status: "executed", message_id: properties.messageID || null };
    normalized.event_type = isTestCommand(command) ? "test" : "command";
    normalized.content = command || null;
    normalized.commands_run = command ? [commandRecord] : [];
    normalized.tests_run = isTestCommand(command) ? [commandRecord] : [];
  } else if (eventType === "file.edited") {
    normalized.event_type = "file_edit";
    normalized.files_changed = properties.file ? [properties.file] : [];
  } else if (eventType === "session.diff") {
    normalized.event_type = "file_edit";
    normalized.files_changed = Array.isArray(properties.diff) ? properties.diff.map((item) => item.file).filter(Boolean) : [];
  } else if (eventType === "session.error") {
    normalized.event_type = "error";
    normalized.content = stringifyEvent(properties.error || properties);
    normalized.final_status = "error";
  } else if (eventType === "session.idle") {
    normalized.event_type = "final";
    normalized.final_status = "unknown";
  } else {
    normalized.event_type = "tool_result";
    normalized.content = stringifyEvent(properties || payload || event);
  }
}

function applyOpenCodePart(normalized, part) {
  normalized.content = extractText(part) || normalized.content;
  if (part.type === "tool") {
    normalized.event_type = part.state?.status === "completed" || part.state?.status === "error" ? "tool_result" : "tool_call";
    normalized.tool_name = part.tool || null;
    normalized.tool_args = part.state?.input || null;
    normalized.observation = part.state?.output || part.state?.error || part.state?.raw || null;
    if (part.tool === "bash") {
      const command = String(part.state?.input?.command || part.state?.input?.cmd || part.state?.input?.args || "");
      const commandRecord = {
        command,
        code: part.state?.metadata?.exit_code ?? part.state?.metadata?.exitCode ?? null,
        status: part.state?.status || null,
      };
      normalized.event_type = isTestCommand(command) ? "test" : "command";
      normalized.content = command || normalized.content;
      normalized.commands_run = command ? [commandRecord] : [];
      normalized.tests_run = isTestCommand(command) ? [commandRecord] : [];
    }
  } else if (part.type === "patch") {
    normalized.event_type = "file_edit";
    normalized.files_changed = Array.isArray(part.files) ? part.files : [];
  } else if (part.type === "reasoning" || part.type === "text") {
    normalized.event_type = "tool_result";
  } else if (part.type === "step-finish") {
    normalized.event_type = "tool_result";
    normalized.audit = {
      tokens_input: part.tokens?.input ?? null,
      tokens_output: part.tokens?.output ?? null,
      tokens_reasoning: part.tokens?.reasoning ?? null,
      cost: part.cost ?? null,
    };
  }
}

function questionForCodexServerRequest(request) {
  const params = request?.params || {};
  if (Array.isArray(params.questions)) {
    return params.questions
      .map((question) => `${question?.header ? `${question.header}: ` : ""}${question?.question || ""}`.trim())
      .filter(Boolean)
      .join("\n");
  }
  return params.reason || params.message || params.url || null;
}

function applyHumanInputRequest(normalized, event, payload) {
  const request = event.request || {};
  const requestType = event.request_type || request.request_type || requestTypeForNativeEvent(event.native_event_type);
  normalized.event_type = requestEventType(requestType);
  normalized.native_event_type = event.native_event_type || request.native_event_type || null;
  normalized.native_payload = payload;
  normalized.normalized_request_type = requestType || "unknown";
  normalized.question = event.question || request.normalized_question || request.question || null;
  normalized.tool_args = {
    request_id: event.request_id || request.request_id || null,
    options: event.options || request.options || [],
    context: event.context || request.context || {},
  };
}

function applyAskHumanResult(normalized, event) {
  const result = event.result || {};
  const requestType = event.request_type || result.request_type || "clarification";
  normalized.event_type = requestType === "elicitation" ? "clarification_answer" : `${requestType}_answer`;
  normalized.native_event_type = event.native_event_type || null;
  normalized.native_payload = result;
  normalized.normalized_request_type = requestType;
  normalized.answer = result.resolution || null;
  normalized.ask_human_status = result.status || "unknown";
  normalized.matched_blocker_ids =
    result.blocker_id && result.blocker_id !== "UNKNOWN" ? [result.blocker_id] : Array.isArray(result.matched_blocker_ids) ? result.matched_blocker_ids : [];
  normalized.matched_source_ids = result.source?.source_id ? [result.source.source_id] : [];
  normalized.audit = {
    prompt_hash: result.oracle?.prompt_hash || null,
    kb_hash: result.source?.kb_hash || null,
    model_id: result.oracle?.model_id || null,
    cache_hit: Boolean(result.cache?.hit),
    cache_key: result.cache?.key || null,
  };
}

function applyApprovalResult(normalized, event) {
  const requestType = event.request_type || "approval";
  const decision = event.decision || {};
  normalized.event_type = requestType === "permission" ? "permission_result" : "approval_result";
  normalized.native_event_type = event.native_event_type || null;
  normalized.native_payload = decision;
  normalized.normalized_request_type = requestType;
  normalized.approval_decision = decision.allowed ? "approved" : "denied";
  normalized.approval_grounding = decision.grounding || decision.source || "fallback";
  normalized.answer = decision.reason || null;
  normalized.audit = {
    kb_hash: decision.kb_hash || null,
    approval_id: decision.approval_id || null,
    registry_status: decision.registry_status || null,
  };
}

function requestEventType(requestType) {
  if (requestType === "approval") return "approval_request";
  if (requestType === "permission") return "permission_request";
  if (requestType === "unknown") return "tool_call";
  return "clarification_request";
}

function requestTypeForNativeEvent(nativeEventType) {
  const text = String(nativeEventType || "");
  if (/permissions/i.test(text)) return "permission";
  if (/approval|canUseTool|applyPatchApproval|execCommandApproval/i.test(text)) return "approval";
  if (/elicitation/i.test(text)) return "elicitation";
  if (/requestUserInput|AskUserQuestion|ask_human/i.test(text)) return "clarification";
  return "unknown";
}

function eventTypeForNativeRequest(method) {
  const requestType = requestTypeForNativeEvent(method);
  return requestEventType(requestType);
}

function isTestCommand(command) {
  return /(^|\s)(npm\s+test|npm\s+run\s+test|node\s+--test|pytest|python3?\s+-m\s+(pytest|unittest)|make\s+test|cargo\s+test|go\s+test)\b/.test(
    String(command || "")
  );
}

function filesFromDiff(diff) {
  const files = [];
  for (const line of String(diff || "").split(/\r?\n/)) {
    const match = /^diff --git a\/(.+?) b\/(.+)$/.exec(line);
    if (match) files.push(match[2]);
  }
  return [...new Set(files)];
}

function findClaudeContent(message, type) {
  const content = message?.message?.content || message?.content;
  if (!Array.isArray(content)) return null;
  return content.find((item) => item?.type === type) || null;
}

function extractText(value) {
  if (!value) return null;
  if (typeof value === "string") return value;
  if (typeof value.text === "string") return value.text;
  const content = value.message?.content || value.content;
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    const parts = content
      .map((item) => {
        if (typeof item === "string") return item;
        if (typeof item?.text === "string") return item.text;
        if (typeof item?.content === "string") return item.content;
        if (Array.isArray(item?.content)) return item.content.map((part) => part?.text || "").join("");
        return "";
      })
      .filter(Boolean);
    return parts.length ? parts.join("\n") : null;
  }
  return null;
}

function stringifyEvent(value) {
  if (value == null) return null;
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}
