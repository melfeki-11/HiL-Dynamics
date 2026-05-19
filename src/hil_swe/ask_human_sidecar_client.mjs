import http from "node:http";
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  CANT_ANSWER,
  UNKNOWN_BLOCKER_ID,
} from "../shared/human_input.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
export const ASK_HUMAN_SIDECAR_SCRIPT = path.join(__dirname, "ask_human_sidecar.mjs");

export function fallbackSidecarResult({
  question = "",
  requestType = "clarification",
  nativeEventType = "unknown.ask_human",
  rawEvent = {},
  context = {},
  source = "sidecar_error_fallback",
} = {}) {
  const requestId = `${String(nativeEventType || "ask_human").replace(/[^a-zA-Z0-9_]+/g, "_")}_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
  const normalizedQuestion = String(question ?? "");
  return {
    resolution: CANT_ANSWER,
    selected_labels: [CANT_ANSWER],
    blocker_id: UNKNOWN_BLOCKER_ID,
    status: "error",
    events: [
      {
        type: "human_input_raw_event",
        timestamp: new Date().toISOString(),
        request_id: requestId,
        request_type: requestType,
        native_event_type: nativeEventType,
        question: normalizedQuestion,
        options: [],
        context: { ...context, source },
        raw_event: rawEvent || { question: normalizedQuestion },
      },
      {
        type: "human_input_result",
        timestamp: new Date().toISOString(),
        request_id: requestId,
        request_type: requestType,
        native_event_type: nativeEventType,
        result: {
          resolution: CANT_ANSWER,
          selected_labels: [CANT_ANSWER],
          blocker_id: UNKNOWN_BLOCKER_ID,
          status: "error",
        },
      },
    ],
  };
}

export async function sidecarAsk({
  sidecarUrl,
  question = "",
  options = [],
  context = {},
  requestType = "clarification",
  nativeEventType = "unknown.ask_human",
  rawEvent = {},
  timeoutMs = 1_200_000,
  fallbackSource = "sidecar_error_fallback",
} = {}) {
  const payload = JSON.stringify({
    question,
    options,
    context,
    request_type: requestType,
    native_event_type: nativeEventType,
    raw_event: rawEvent || { question },
  });

  return new Promise((resolve) => {
    let urlObj;
    try {
      urlObj = new URL(`${sidecarUrl}/ask`);
    } catch {
      resolve(fallbackSidecarResult({ question, requestType, nativeEventType, rawEvent, context, source: fallbackSource }));
      return;
    }

    const req = http.request(
      {
        hostname: urlObj.hostname,
        port: urlObj.port || 80,
        path: urlObj.pathname,
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(payload),
        },
      },
      (res) => {
        let data = "";
        res.on("data", (chunk) => { data += chunk; });
        res.on("end", () => {
          try {
            const parsed = JSON.parse(data || "{}");
            resolve({
              resolution: String(parsed.resolution ?? CANT_ANSWER),
              selected_labels: Array.isArray(parsed.selected_labels)
                ? parsed.selected_labels
                : [String(parsed.resolution ?? CANT_ANSWER)],
              blocker_id: String(parsed.blocker_id ?? UNKNOWN_BLOCKER_ID),
              status: String(parsed.status ?? "unknown"),
              events: Array.isArray(parsed.events) ? parsed.events : [],
            });
          } catch {
            resolve(fallbackSidecarResult({ question, requestType, nativeEventType, rawEvent, context, source: fallbackSource }));
          }
        });
      },
    );

    req.on("error", () => {
      resolve(fallbackSidecarResult({ question, requestType, nativeEventType, rawEvent, context, source: fallbackSource }));
    });
    req.setTimeout(timeoutMs, () => {
      req.destroy();
      resolve(fallbackSidecarResult({ question, requestType, nativeEventType, rawEvent, context, source: fallbackSource }));
    });
    req.write(payload);
    req.end();
  });
}

export function httpGetJson(url, timeoutMs = 10_000) {
  return new Promise((resolve, reject) => {
    const req = http.get(url, (res) => {
      let data = "";
      res.on("data", (chunk) => { data += chunk; });
      res.on("end", () => {
        try {
          resolve(JSON.parse(data || "{}"));
        } catch (err) {
          reject(err);
        }
      });
    });
    req.on("error", reject);
    req.setTimeout(timeoutMs, () => req.destroy(new Error("GET timeout")));
  });
}

export function httpPostJson(url, body = {}, timeoutMs = 10_000) {
  return new Promise((resolve, reject) => {
    const payload = JSON.stringify(body);
    const u = new URL(url);
    const req = http.request(
      {
        method: "POST",
        hostname: u.hostname,
        port: u.port || 80,
        path: u.pathname + u.search,
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(payload),
        },
      },
      (res) => {
        let data = "";
        res.on("data", (chunk) => { data += chunk; });
        res.on("end", () => {
          try {
            resolve(JSON.parse(data || "{}"));
          } catch (err) {
            reject(err);
          }
        });
      },
    );
    req.on("error", reject);
    req.setTimeout(timeoutMs, () => req.destroy(new Error("POST timeout")));
    req.write(payload);
    req.end();
  });
}

export async function startAskHumanSidecar({
  uid,
  mode,
  taskDir,
  workspace,
  askHumanBaseUrl = "",
  askHumanModel = "",
  script = ASK_HUMAN_SIDECAR_SCRIPT,
  cwd = workspace || process.cwd(),
  stderrPrefix = "[ask_human_sidecar]",
  env = process.env,
} = {}) {
  const proc = spawn(process.execPath, [script], {
    env: {
      ...env,
      MODE: mode || env.MODE || "ask_human",
      TASK_DIR: taskDir || env.TASK_DIR || "/task",
      TASK_UID: uid || env.TASK_UID || "unknown",
      ASK_HUMAN_BASE_URL: askHumanBaseUrl || env.ASK_HUMAN_BASE_URL || "",
      ASK_HUMAN_MODEL: askHumanModel || env.ASK_HUMAN_MODEL || "",
    },
    cwd,
    stdio: ["ignore", "pipe", "pipe"],
  });

  proc.stderr.on("data", (chunk) => {
    if (stderrPrefix) process.stderr.write(`${stderrPrefix} ${String(chunk)}`);
  });

  let buf = "";
  const portLine = await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      reject(new Error(`sidecar startup timeout after 20000ms. output=${buf}`));
    }, 20_000);

    const onData = (chunk) => {
      buf += String(chunk);
      const idx = buf.indexOf("\n");
      if (idx < 0) return;
      clearTimeout(timeout);
      proc.stdout.off("data", onData);
      resolve(buf.slice(0, idx).trim());
    };

    proc.stdout.on("data", onData);
    proc.once("exit", (code) => {
      clearTimeout(timeout);
      reject(new Error(`sidecar exited with code ${code} before announcing port. buf=${buf}`));
    });
    proc.once("error", (err) => {
      clearTimeout(timeout);
      reject(new Error(`sidecar spawn error: ${err.message}`));
    });
  });

  const match = /^SIDECAR_PORT=(\d+)$/.exec(String(portLine || ""));
  if (!match) throw new Error(`sidecar did not announce port. got=${portLine}`);
  const url = `http://127.0.0.1:${Number(match[1])}`;

  for (let i = 0; i < 20; i++) {
    try {
      const health = await httpGetJson(`${url}/health`, 5_000);
      if (health?.ok) return { proc, url };
    } catch {
      // keep polling
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error("sidecar health check failed after 20 attempts");
}

export function stopSidecar(proc) {
  if (!proc) return;
  try {
    proc.kill("SIGTERM");
  } catch {
    // ignore shutdown races
  }
}
