import { execFile } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

export const rootDir = path.resolve(new URL("../..", import.meta.url).pathname);
export const autonomyCalibrationRoot = process.env.AUTONOMY_CALIBRATION_ROOT || "";

// Find the credentials .env file.  Check locations in priority order so the
// right file is picked up on any developer's machine or in CI without extra setup:
//   1. LITELLM_CREDENTIALS_FILE env var (explicit override)
//   2. trust_horizon/.env  (conventional location in this repo)
function _findCredentialsFile() {
  if (process.env.LITELLM_CREDENTIALS_FILE) return process.env.LITELLM_CREDENTIALS_FILE;
  const candidates = [
    path.join(rootDir, ".env"),
  ];
  for (const p of candidates) {
    if (fs.existsSync(p)) return p;
  }
  return candidates[0]; // return trust_horizon/.env as the "intended" path even if missing
}
export const DEFAULT_LITELLM_CREDENTIALS_FILE = _findCredentialsFile();
export const dataDir = path.join(rootDir, "data");
export const evalsDir = path.join(rootDir, "evals");
export const vendorDir = process.env.SWEBENCH_PRO_VENDOR_DIR || path.join(autonomyCalibrationRoot, "vendor", "SWE-bench_Pro-os");

export const DEFAULT_BASE_URL = "";
export const DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6";
export const DEFAULT_CODEX_MODEL = "gpt-5.5";
export const DEFAULT_CODEX_REASONING_EFFORT = "low";
export const DEFAULT_OPENCODE_MODEL = "bedrock/qwen.qwen3-32b-v1:0";
export const DEFAULT_OPENCODE_PROVIDER = "litellm";
export const PAPER_ASK_HUMAN_MODEL = "casperhansen/llama-3.3-70b-instruct-awq";
export const PAPER_ASK_HUMAN_MODEL_BEDROCK = "bedrock/meta.llama3-3-70b-instruct-v1:0";
export const DEFAULT_ASK_HUMAN_MODEL = PAPER_ASK_HUMAN_MODEL_BEDROCK;
export const DEFAULT_ASK_HUMAN_SEED = 20260501;
export const AWS_SECRET_ID = process.env.LITELLM_AWS_SECRET_ID || process.env.AWS_SECRET_ID || "";
export const AWS_REGION = process.env.AWS_REGION || "";
export const DEFAULT_AWS_PROFILE = "";
export const CODEX_LITELLM_PROVIDER = "litellm";

let liteLLMKeyPromise = null;
let liteLLMEnvLoaded = false;

function parseEnvFile(text) {
  const out = {};
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const match = /^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$/.exec(line);
    if (!match) continue;
    let value = match[2].trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    out[match[1]] = value;
  }
  return out;
}

export function ensureLiteLLMEnvLoaded({ required = false } = {}) {
  if (liteLLMEnvLoaded) return { loaded: false, path: DEFAULT_LITELLM_CREDENTIALS_FILE, keys: [] };
  liteLLMEnvLoaded = true;
  const envFile = DEFAULT_LITELLM_CREDENTIALS_FILE;
  if (!envFile || !fs.existsSync(envFile)) {
    if (required) throw new Error(`Missing LiteLLM credential env file: ${envFile}`);
    return { loaded: false, path: envFile, keys: [] };
  }
  const parsed = parseEnvFile(fs.readFileSync(envFile, "utf8"));
  const keys = [];
  for (const [key, value] of Object.entries(parsed)) {
    if (process.env[key] === undefined && value !== "") {
      process.env[key] = value;
      keys.push(key);
    }
  }
  return { loaded: true, path: envFile, keys };
}

export function cpuCount() {
  return os.availableParallelism?.() || os.cpus()?.length || 1;
}

export function defaultGenerateConcurrency(totalJobs) {
  if (totalJobs <= 0) return 0;
  const fromEnv = Number(process.env.HARNESS_CONCURRENCY || "");
  if (Number.isInteger(fromEnv) && fromEnv > 0) return Math.min(totalJobs, fromEnv);
  const maxConcurrency = Number(process.env.HARNESS_MAX_CONCURRENCY || 8);
  const cap = Number.isInteger(maxConcurrency) && maxConcurrency > 0 ? maxConcurrency : 8;
  const workerMemoryGb = Number(process.env.HARNESS_GENERATE_WORKER_MEMORY_GB || 12);
  const memoryBased =
    Number.isFinite(workerMemoryGb) && workerMemoryGb > 0
      ? Math.max(1, Math.floor(os.totalmem() / (workerMemoryGb * 1024 ** 3)))
      : Number.POSITIVE_INFINITY;
  return Math.min(totalJobs, cap, Math.max(1, Math.floor(cpuCount() / 24)), memoryBased);
}

export function defaultEvalWorkers() {
  const fromEnv = Number(process.env.SWEBENCH_EVAL_WORKERS || "");
  if (Number.isInteger(fromEnv) && fromEnv > 0) return fromEnv;
  return Math.min(8, Math.max(1, Math.floor(cpuCount() / 32)));
}

export function getBaseUrl() {
  ensureLiteLLMEnvLoaded();
  const baseUrl = process.env.LITELLM_BASE_URL || process.env.ANTHROPIC_BASE_URL || process.env.OPENAI_BASE_URL || DEFAULT_BASE_URL;
  if (!baseUrl) {
    throw new Error("Missing LITELLM_BASE_URL or ANTHROPIC_BASE_URL. Source the local credential/config file before live runs.");
  }
  return baseUrl;
}

export function getResponsesBaseUrl() {
  const baseUrl = getBaseUrl().replace(/\/+$/, "");
  return baseUrl.endsWith("/v1") ? baseUrl : `${baseUrl}/v1`;
}

async function readAwsSecretKey(secretKeyName) {
  ensureLiteLLMEnvLoaded();
  const secretId = process.env.LITELLM_AWS_SECRET_ID || process.env.AWS_SECRET_ID || AWS_SECRET_ID;
  const region = process.env.AWS_REGION || AWS_REGION;
  if (!secretId) {
    throw new Error("Missing LITELLM_AWS_SECRET_ID for AWS Secrets Manager lookup.");
  }
  if (!secretKeyName) {
    throw new Error("Missing LITELLM_AWS_SECRET_KEY for AWS Secrets Manager lookup.");
  }
  const args = [
    "secretsmanager",
    "get-secret-value",
    "--secret-id",
    secretId,
    "--query",
    "SecretString",
    "--output",
    "text",
  ];
  if (region) args.splice(4, 0, "--region", region);
  if (process.env.AWS_PROFILE || DEFAULT_AWS_PROFILE) args.splice(0, 0, "--profile", process.env.AWS_PROFILE || DEFAULT_AWS_PROFILE);
  const { stdout } = await execFileAsync("aws", args, { maxBuffer: 10 * 1024 * 1024 });
  const parsed = JSON.parse(stdout);
  return parsed[secretKeyName];
}

export async function getLiteLLMKey() {
  ensureLiteLLMEnvLoaded();
  const litellmEnvKey = process.env.LITELLM_PROXY_API_KEY || process.env.LITELLM_API_KEY;
  if (litellmEnvKey) return litellmEnvKey;
  const secretKeyName = process.env.LITELLM_AWS_SECRET_KEY || process.env.AWS_SECRET_KEY_NAME || "";
  const secretId = process.env.LITELLM_AWS_SECRET_ID || process.env.AWS_SECRET_ID || "";
  if (secretId && secretKeyName) {
    if (!liteLLMKeyPromise) {
      liteLLMKeyPromise = readAwsSecretKey(secretKeyName).catch((error) => {
        liteLLMKeyPromise = null;
        throw error;
      });
    }
    const key = await liteLLMKeyPromise;
    if (key) return key;
    liteLLMKeyPromise = null;
  }
  const ambientProviderKey = process.env.ANTHROPIC_AUTH_TOKEN || process.env.OPENAI_API_KEY;
  if (ambientProviderKey) return ambientProviderKey;
  if (liteLLMKeyPromise) return liteLLMKeyPromise;
  liteLLMKeyPromise = readAwsSecretKey(secretKeyName).catch((error) => {
    liteLLMKeyPromise = null;
    throw error;
  });
  const key = await liteLLMKeyPromise;
  if (!key) {
    liteLLMKeyPromise = null;
    throw new Error(
      "Could not find LiteLLM key in ANTHROPIC_AUTH_TOKEN, LITELLM_PROXY_API_KEY, LITELLM_API_KEY, OPENAI_API_KEY, or the configured AWS Secrets Manager entry."
    );
  }
  return key;
}

export async function claudeEnv(extra = {}) {
  const token = await getLiteLLMKey();
  const baseUrl = getBaseUrl();
  return {
    ...process.env,
    ...extra,
    ...(process.env.AWS_PROFILE || DEFAULT_AWS_PROFILE ? { AWS_PROFILE: process.env.AWS_PROFILE || DEFAULT_AWS_PROFILE } : {}),
    ANTHROPIC_AUTH_TOKEN: token,
    ANTHROPIC_BASE_URL: baseUrl,
    LITELLM_BASE_URL: baseUrl,
  };
}

export async function codexClientOptions(extraEnv = {}) {
  const token = await getLiteLLMKey();
  const baseUrl = getBaseUrl();
  const responsesBaseUrl = getResponsesBaseUrl();
  return {
    apiKey: token,
    env: {
      ...process.env,
      ...extraEnv,
      ...(process.env.AWS_PROFILE || DEFAULT_AWS_PROFILE ? { AWS_PROFILE: process.env.AWS_PROFILE || DEFAULT_AWS_PROFILE } : {}),
      CODEX_API_KEY: token,
      OPENAI_API_KEY: token,
      LITELLM_BASE_URL: baseUrl,
      CODEX_LITELLM_BASE_URL: responsesBaseUrl,
    },
    config: {
      approval_policy: "never",
      sandbox_mode: "workspace-write",
      sandbox_workspace_write: { network_access: true },
      model_provider: CODEX_LITELLM_PROVIDER,
      model_providers: {
        [CODEX_LITELLM_PROVIDER]: {
          name: "LiteLLM",
          base_url: responsesBaseUrl,
          env_key: "CODEX_API_KEY",
          wire_api: "responses",
          requires_openai_auth: true,
        },
      },
    },
  };
}

function opencodeModelId(model) {
  const value = model || DEFAULT_OPENCODE_MODEL;
  if (value.startsWith(`${DEFAULT_OPENCODE_PROVIDER}/`)) return value.slice(DEFAULT_OPENCODE_PROVIDER.length + 1);
  return value;
}

export async function opencodeEnv(extra = {}) {
  const token = await getLiteLLMKey();
  const baseUrl = getResponsesBaseUrl();
  return {
    ...process.env,
    ...extra,
    LITELLM_API_KEY: token,
    LITELLM_PROXY_API_KEY: token,
    OPENAI_API_KEY: token,
    LITELLM_BASE_URL: getBaseUrl(),
    OPENCODE_LITELLM_BASE_URL: baseUrl,
  };
}

export async function opencodeConfig({ model = DEFAULT_OPENCODE_MODEL, mcp = {}, agentPrompt = "" } = {}) {
  const token = await getLiteLLMKey();
  const baseUrl = getResponsesBaseUrl();
  const modelId = opencodeModelId(model);
  return {
    "$schema": "https://opencode.ai/config.json",
    model: `${DEFAULT_OPENCODE_PROVIDER}/${modelId}`,
    small_model: `${DEFAULT_OPENCODE_PROVIDER}/${modelId}`,
    enabled_providers: [DEFAULT_OPENCODE_PROVIDER],
    disabled_providers: [],
    autoupdate: false,
    share: "disabled",
    provider: {
      [DEFAULT_OPENCODE_PROVIDER]: {
        npm: "@ai-sdk/openai-compatible",
        name: "LiteLLM",
        options: {
          baseURL: baseUrl,
          apiKey: "{env:LITELLM_API_KEY}",
        },
        models: {
          [modelId]: {
            name: modelId,
            tool_call: true,
            reasoning: true,
          },
        },
      },
    },
    agent: {
      build: {
        model: `${DEFAULT_OPENCODE_PROVIDER}/${modelId}`,
        mode: "primary",
        prompt: agentPrompt || undefined,
        permission: {
          edit: "ask",
          bash: "ask",
          webfetch: "deny",
          external_directory: "deny",
        },
      },
    },
    permission: {
      edit: "ask",
      bash: "ask",
      webfetch: "deny",
      external_directory: "deny",
    },
    mcp,
  };
}
