/**
 * End-to-end test: verify the ask_human LLM judge routing works against
 * the vLLM server (casperhansen/llama-3.3-70b-instruct-awq).
 *
 * Start the vLLM server first (separate tmux session):
 *   CUDA_VISIBLE_DEVICES=0,1,2,3 python3 models/research_evals/hil_bench/utils/serve_ask_human_vllm.py \
 *     --model casperhansen/llama-3.3-70b-instruct-awq --host 0.0.0.0 --port 8808 \
 *     --tmpdir /dev/shm --max-model-len 4096 --tp 4 --pp 1 --gpu-memory-utilization 0.7
 *
 * Usage (from trust_horizon/):
 *   node tests/test_vllm_routing.mjs [--uid 69c6ba856585e74e8ba6e0bc]
 *   # (ASK_HUMAN_BASE_URL and ASK_HUMAN_MODEL are read from .env via the harness,
 *   #  or set them explicitly for a standalone run)
 *
 * The script will:
 *   1. Load a real blocker registry from disk
 *   2. Route a question that SHOULD match a blocker → expect status="answered"
 *   3. Route a question that SHOULD NOT match   → expect status="unknown"
 *   4. Print pass/fail and the raw oracle output for inspection
 *
 * This exercises the exact same code path that run_claude.mjs and run_codex.mjs
 * use when an agent calls AskUserQuestion / requestUserInput.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { askHuman, createAskHumanRequest, loadHumanKnowledgeBase, UNKNOWN_BLOCKER_ID, UNKNOWN_RESOLUTION } from "../src/shared/human_input.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT      = path.resolve(__dirname, "..");
const DATA_ROOT = path.join(ROOT, "data", "hil_bench_swe", "tasks");

// Load trust_horizon/.env into process.env (only fills gaps — does not override existing env vars).
// This mirrors run_hil_swe.py's dotenv loading so the test works with `node tests/test_vllm_routing.mjs`.
try {
  const envText = fs.readFileSync(path.join(ROOT, ".env"), "utf8");
  for (const line of envText.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq < 1) continue;
    const key = trimmed.slice(0, eq).trim();
    const raw = trimmed.slice(eq + 1).trim().replace(/^["']|["']$/g, "");
    if (key && !(key in process.env)) process.env[key] = raw;
  }
} catch { /* .env is optional */ }

// ── Config (env vars, same as run_claude.mjs / run_codex.mjs) ─────────────────

const ASK_HUMAN_BASE_URL = (process.env.ASK_HUMAN_BASE_URL || "").trim();
const ASK_HUMAN_MODEL    = (process.env.ASK_HUMAN_MODEL    || "").trim();

// Allow overriding the UID via --uid <hex>
const uidFlag = process.argv.indexOf("--uid");
const UID = uidFlag >= 0 ? process.argv[uidFlag + 1] : "69c6ba856585e74e8ba6e0bc";

if (!ASK_HUMAN_BASE_URL) {
  console.error("ERROR: ASK_HUMAN_BASE_URL is not set.");
  console.error("  Set it to point at the running vLLM server, e.g.:");
  console.error("  ASK_HUMAN_BASE_URL=http://localhost:8808/v1 node tests/test_vllm_routing.mjs");
  process.exit(1);
}
if (!ASK_HUMAN_MODEL) {
  console.error("ERROR: ASK_HUMAN_MODEL is not set.");
  console.error("  Set it to the model the vLLM server is serving, e.g.:");
  console.error("  ASK_HUMAN_MODEL=casperhansen/llama-3.3-70b-instruct-awq node tests/test_vllm_routing.mjs");
  process.exit(1);
}

// ── Test helpers ──────────────────────────────────────────────────────────────

let passed = 0;
let failed = 0;

function check(label, condition, detail = "") {
  if (condition) {
    console.log(`  ✅ PASS  ${label}`);
    passed++;
  } else {
    console.error(`  ❌ FAIL  ${label}${detail ? `\n         ${detail}` : ""}`);
    failed++;
  }
}

// ── Main ──────────────────────────────────────────────────────────────────────

const kbPath = path.join(DATA_ROOT, UID, "blocker_registry.json");
console.log(`\n=== vLLM routing E2E test ===`);
console.log(`UID:           ${UID}`);
console.log(`KB path:       ${kbPath}`);
console.log(`Judge URL:     ${ASK_HUMAN_BASE_URL}`);
console.log(`Judge model:   ${ASK_HUMAN_MODEL}`);
console.log();

// Verify registry loads correctly
let kb;
try {
  kb = await loadHumanKnowledgeBase(kbPath);
  console.log(`Loaded ${kb.entries.length} blockers from registry:`);
  for (const e of kb.entries) {
    console.log(`  - ${e.blocker_id}: ${String(e.description).trim().slice(0, 60)}...`);
  }
  console.log();
} catch (err) {
  console.error(`FATAL: Could not load blocker registry at ${kbPath}: ${err.message}`);
  process.exit(1);
}

// Test 1: question that SHOULD match blocker "runtime_replacement_import_path"
// (one of the exact trigger_questions from the registry)
const MATCHING_QUESTION = "What exact import path should replace the legacy shim in runtime module and module_utils files?";
const MATCHING_BLOCKER  = "runtime_replacement_import_path";

console.log(`--- Test 1: matching question ---`);
console.log(`Question: "${MATCHING_QUESTION}"`);
console.log(`Expected blocker: ${MATCHING_BLOCKER}`);
let result1;
try {
  result1 = await askHuman({
    request: createAskHumanRequest({ instanceId: UID, requestType: "clarification", question: MATCHING_QUESTION }),
    registry: kb,
    cachePath: null,
    modelId: ASK_HUMAN_MODEL,
    baseUrl: ASK_HUMAN_BASE_URL,
  });
  console.log(`  result.status:    ${result1.status}`);
  console.log(`  result.blocker_id: ${result1.blocker_id}`);
  console.log(`  oracle.reason:    ${result1.oracle?.reason ?? "(none)"}`);
  if (result1.status === "answered") {
    console.log(`  resolution:       ${String(result1.resolution).slice(0, 80)}...`);
  }
  check("status === 'answered'", result1.status === "answered", `got status=${result1.status}, reason=${result1.oracle?.reason}`);
  check(`blocker_id === '${MATCHING_BLOCKER}'`, result1.blocker_id === MATCHING_BLOCKER, `got ${result1.blocker_id}`);
} catch (err) {
  console.error(`  EXCEPTION: ${err.message}`);
  check("no exception thrown", false, err.message);
}

console.log();

// Test 2: question that SHOULD NOT match any blocker
const IRRELEVANT_QUESTION = "What is the Python version that the project targets?";

console.log(`--- Test 2: irrelevant question ---`);
console.log(`Question: "${IRRELEVANT_QUESTION}"`);
console.log(`Expected: status="unknown" (no match)`);
let result2;
try {
  result2 = await askHuman({
    request: createAskHumanRequest({ instanceId: UID, requestType: "clarification", question: IRRELEVANT_QUESTION }),
    registry: kb,
    cachePath: null,
    modelId: ASK_HUMAN_MODEL,
    baseUrl: ASK_HUMAN_BASE_URL,
  });
  console.log(`  result.status:    ${result2.status}`);
  console.log(`  result.blocker_id: ${result2.blocker_id}`);
  console.log(`  oracle.reason:    ${result2.oracle?.reason ?? "(none)"}`);
  console.log(`  resolution:       ${result2.resolution}`);
  check("status === 'unknown'", result2.status === "unknown", `got status=${result2.status}`);
  check(`blocker_id === '${UNKNOWN_BLOCKER_ID}'`, result2.blocker_id === UNKNOWN_BLOCKER_ID, `got ${result2.blocker_id}`);
  check(`resolution === '${UNKNOWN_RESOLUTION}'`, result2.resolution === UNKNOWN_RESOLUTION, `got ${result2.resolution}`);
} catch (err) {
  console.error(`  EXCEPTION: ${err.message}`);
  check("no exception thrown", false, err.message);
}

// ── Summary ───────────────────────────────────────────────────────────────────
console.log();
console.log(`=== Results: ${passed} passed, ${failed} failed ===`);
if (failed > 0) process.exit(1);
