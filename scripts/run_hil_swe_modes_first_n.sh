#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/run_hil_swe_modes_first_n.sh <num_samples>

Runs the deterministic HiL-Bench SWE first-N fixture in two legacy modes:
  - full_info: blocker resolutions are included upfront; no ask_human KB is wired
  - neutral: blockers stay hidden and the ask_human/request_user_input router is wired

The script uses the local autonomy_calibration data/vendor defaults unless
AUTONOMY_CALIBRATION_ROOT or SWEBENCH_PRO_VENDOR_DIR override them.
EOF
}

if [[ $# -ne 1 ]]; then
  usage
  exit 2
fi

NUM_SAMPLES="$1"
if [[ ! "$NUM_SAMPLES" =~ ^[1-9][0-9]*$ ]]; then
  echo "num_samples must be a positive integer: $NUM_SAMPLES" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

K=3
CREDENTIALS_ENV="${LITELLM_CREDENTIALS_ENV:-}"
CLARIFICATION_PROFILE="generic-v1"
CLAUDE_MODEL_CANDIDATES="claude-opus-4-7"
CLAUDE_THINKING="${CLAUDE_CODE_THINKING:-enabled}"
CLAUDE_EFFORT="${CLAUDE_CODE_EFFORT:-xhigh}"
CODEX_MODEL="gpt-5.5"
CODEX_REASONING_EFFORT="xhigh"
OPENCODE_MODEL="fireworks_ai/glm-5p1"
ASK_HUMAN_MODEL="llmengine/llama-3-3-70b-instruct"
ATTEMPT_TIMEOUT_MS="${HARNESS_ATTEMPT_TIMEOUT_MS:-1800000}"
MAX_TURNS="${HARNESS_MAX_TURNS:-0}"
GENERATE_CONCURRENCY="${HARNESS_CONCURRENCY:-2}"
EVAL_WORKERS="${SWEBENCH_EVAL_WORKERS:-}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
PREPARED_DIR="data/hil_bench_swe_first${NUM_SAMPLES}"
FULL_INFO_RUN_ID="hil-swe-first${NUM_SAMPLES}-all-full-info-${STAMP}"
ASK_HUMAN_RUN_ID="hil-swe-first${NUM_SAMPLES}-all-neutral-${STAMP}"
PREFLIGHT_DIR="evals/hil-swe-first${NUM_SAMPLES}-modes-preflight-${STAMP}"
ASK_HUMAN_CHECK_DIR="${PREPARED_DIR}/ask_human_check_${STAMP}"

if [[ -f "$CREDENTIALS_ENV" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$CREDENTIALS_ENV"
  set +a
fi

echo "HiL-Bench SWE first-${NUM_SAMPLES} all-harness mode run"
echo "  full_info_run_id: ${FULL_INFO_RUN_ID}"
echo "  ask_human_run_id: ${ASK_HUMAN_RUN_ID}"
echo "  prepared_dir: ${PREPARED_DIR}"
echo "  k: ${K}"
echo "  claude-code model candidates: ${CLAUDE_MODEL_CANDIDATES}"
echo "  claude-code thinking: ${CLAUDE_THINKING}"
echo "  claude-code effort: ${CLAUDE_EFFORT}"
echo "  codex model: ${CODEX_MODEL}"
echo "  codex reasoning effort: ${CODEX_REASONING_EFFORT}"
echo "  opencode model: ${OPENCODE_MODEL}"
echo "  ask_human judge model: ${ASK_HUMAN_MODEL}"
echo "  clarification profile: ${CLARIFICATION_PROFILE}"
echo "  attempt timeout ms: ${ATTEMPT_TIMEOUT_MS}"
echo "  max turns: ${MAX_TURNS}"
echo "  generation concurrency: ${GENERATE_CONCURRENCY}"

mkdir -p "$PREFLIGHT_DIR"

echo ""
echo "Preparing deterministic HiL-SWE fixture..."
npm run hil:swe:prepare -- --limit "$NUM_SAMPLES" --out "$PREPARED_DIR"

echo ""
echo "Probing LiteLLM-backed Claude Code, Codex, and ask_human judge routes..."
node scripts/probe_hil_models.mjs \
  --claude-model-candidates "$CLAUDE_MODEL_CANDIDATES" \
  --claude-thinking "$CLAUDE_THINKING" \
  --claude-effort "$CLAUDE_EFFORT" \
  --codex-model "$CODEX_MODEL" \
  --codex-reasoning-effort "$CODEX_REASONING_EFFORT" \
  --opencode-model "$OPENCODE_MODEL" \
  --ask-human-model "$ASK_HUMAN_MODEL" \
  --out "${PREFLIGHT_DIR}/model_probe.json"

CLAUDE_MODEL="$(python3 - "$PREFLIGHT_DIR/model_probe.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["claude"]["accepted_model"])
PY
)"
echo "  accepted claude-code model: ${CLAUDE_MODEL}"

echo ""
echo "Checking registry-backed ask_human behavior..."
npm run hil:swe:check-ask-human -- \
  --kb "${PREPARED_DIR}/kb.json" \
  --manifest "${PREPARED_DIR}/manifest.json" \
  --tasks "$NUM_SAMPLES" \
  --out "$ASK_HUMAN_CHECK_DIR"

COMMON_PASSK_ARGS=(
  --samples "${PREPARED_DIR}/samples.csv"
  --limit "$NUM_SAMPLES"
  --k "$K"
  --harness all
  --claude-model "$CLAUDE_MODEL"
  --claude-thinking "$CLAUDE_THINKING"
  --claude-effort "$CLAUDE_EFFORT"
  --codex-model "$CODEX_MODEL"
  --codex-reasoning-effort "$CODEX_REASONING_EFFORT"
  --opencode-model "$OPENCODE_MODEL"
  --ask-human-model "$ASK_HUMAN_MODEL"
  --attempt-timeout-ms "$ATTEMPT_TIMEOUT_MS"
  --max-turns "$MAX_TURNS"
  --concurrency "$GENERATE_CONCURRENCY"
  --codex-transport app-server
  --codex-approval-policy on-request
)
if [[ -n "$EVAL_WORKERS" ]]; then
  COMMON_PASSK_ARGS+=(--eval-workers "$EVAL_WORKERS")
fi

echo ""
echo "Running full_info k=${K}..."
npm run hil:swe:passk -- \
  --input "${PREPARED_DIR}/input_full_info.jsonl" \
  --run-id "$FULL_INFO_RUN_ID" \
  "${COMMON_PASSK_ARGS[@]}"

python3 scripts/summarize_passk.py --run-id "$FULL_INFO_RUN_ID" --samples "${PREPARED_DIR}/samples.csv" --human-kb "${PREPARED_DIR}/kb.json" --k 1 --k 3
npm run process-metrics -- --run-id "$FULL_INFO_RUN_ID" --human-kb "${PREPARED_DIR}/kb.json"
npm run hil:swe:audit -- --run-id "$FULL_INFO_RUN_ID" --prepared-dir "$PREPARED_DIR"

echo ""
echo "Running ask_human k=${K}..."
npm run hil:swe:passk -- \
  --input "${PREPARED_DIR}/input.jsonl" \
  --run-id "$ASK_HUMAN_RUN_ID" \
  --human-kb "${PREPARED_DIR}/kb.json" \
  --clarification-instruction-profile "$CLARIFICATION_PROFILE" \
  "${COMMON_PASSK_ARGS[@]}"

python3 scripts/summarize_passk.py --run-id "$ASK_HUMAN_RUN_ID" --samples "${PREPARED_DIR}/samples.csv" --human-kb "${PREPARED_DIR}/kb.json" --k 1 --k 3
npm run process-metrics -- --run-id "$ASK_HUMAN_RUN_ID" --human-kb "evals/${ASK_HUMAN_RUN_ID}/human-kb.json"
npm run hil:swe:audit -- --run-id "$ASK_HUMAN_RUN_ID" --prepared-dir "$PREPARED_DIR"

COMBINED_REPORT_DIR="evals/hil-swe-first${NUM_SAMPLES}-modes-report-${STAMP}"
npm run hil:swe:modes-report -- \
  --full-info-run-id "$FULL_INFO_RUN_ID" \
  --ask-human-run-id "$ASK_HUMAN_RUN_ID" \
  --prepared-dir "$PREPARED_DIR" \
  --out-dir "$COMBINED_REPORT_DIR"

echo ""
echo "Mode run complete"
echo "  full_info: evals/${FULL_INFO_RUN_ID}"
echo "  ask_human: evals/${ASK_HUMAN_RUN_ID}"
echo "  combined_report: ${COMBINED_REPORT_DIR}/final_report.md"
