#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/run_hil_swe_first_n.sh <num_samples>

Runs the first N deterministic HiL-Bench SWE samples on:
  - Claude Code: claude-sonnet-4-6
  - Codex: gpt-5.5 with low reasoning effort
  - OpenCode: bedrock/qwen.qwen3-32b-v1:0

The script prepares the HiL-SWE fixture, validates the ask_human KB selector,
runs k=3 for all harnesses, computes pass@1/pass@3 and process metrics, and
prints final per-harness tables.
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
CREDENTIALS_ENV="${LITELLM_CREDENTIALS_ENV:-/mnt/efs/mohamedelfeki/Codes/autonomy_calibration/LOCAL_LITELLM_CREDENTIALS.env}"
CLARIFICATION_PROFILE="generic-v1"
CLAUDE_MODEL_CANDIDATES="claude-sonnet-4-6"
CLAUDE_THINKING="${CLAUDE_CODE_THINKING:-disabled}"
CLAUDE_EFFORT="${CLAUDE_CODE_EFFORT:-low}"
CODEX_MODEL="gpt-5.5"
CODEX_REASONING_EFFORT="low"
OPENCODE_MODEL="bedrock/qwen.qwen3-32b-v1:0"
ASK_HUMAN_MODEL="bedrock/qwen.qwen3-32b-v1:0"
ATTEMPT_TIMEOUT_MS="${HARNESS_ATTEMPT_TIMEOUT_MS:-1800000}"
MAX_TURNS="${HARNESS_MAX_TURNS:-200}"
EVAL_WORKERS="${SWEBENCH_EVAL_WORKERS:-}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
PREPARED_DIR="data/hil_bench_swe_first${NUM_SAMPLES}"
RUN_ID="hil-swe-first${NUM_SAMPLES}-all-ask-human-${STAMP}"
RUN_DIR="evals/${RUN_ID}"
ASK_HUMAN_CHECK_DIR="${PREPARED_DIR}/ask_human_check_${STAMP}"

if [[ -f "$CREDENTIALS_ENV" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$CREDENTIALS_ENV"
  set +a
fi

echo "HiL-Bench SWE first-${NUM_SAMPLES} live pilot"
echo "  run_id: ${RUN_ID}"
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

mkdir -p "${RUN_DIR}/preflight"

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
  --out "${RUN_DIR}/preflight/model_probe.json"

CLAUDE_MODEL="$(python3 - "$RUN_DIR/preflight/model_probe.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["claude"]["accepted_model"])
PY
)"
echo "  accepted claude-code model: ${CLAUDE_MODEL}"

echo ""
echo "Preparing deterministic HiL-SWE fixture..."
npm run hil:swe:prepare -- --limit "$NUM_SAMPLES" --out "$PREPARED_DIR"

echo ""
echo "Checking registry-backed ask_human behavior..."
npm run hil:swe:check-ask-human -- \
  --kb "${PREPARED_DIR}/kb.json" \
  --manifest "${PREPARED_DIR}/manifest.json" \
  --tasks "$NUM_SAMPLES" \
  --out "$ASK_HUMAN_CHECK_DIR"

echo ""
echo "Running all harnesses k=${K} live pilot..."
PASSK_ARGS=(
  --input "${PREPARED_DIR}/input.jsonl"
  --samples "${PREPARED_DIR}/samples.csv"
  --limit "$NUM_SAMPLES"
  --k "$K"
  --harness all
  --run-id "$RUN_ID"
  --human-kb "${PREPARED_DIR}/kb.json"
  --clarification-instruction-profile "$CLARIFICATION_PROFILE"
  --claude-model "$CLAUDE_MODEL"
  --claude-thinking "$CLAUDE_THINKING"
  --claude-effort "$CLAUDE_EFFORT"
  --codex-model "$CODEX_MODEL"
  --codex-reasoning-effort "$CODEX_REASONING_EFFORT"
  --opencode-model "$OPENCODE_MODEL"
  --ask-human-model "$ASK_HUMAN_MODEL"
  --codex-transport app-server
  --codex-approval-policy on-request
  --attempt-timeout-ms "$ATTEMPT_TIMEOUT_MS"
  --max-turns "$MAX_TURNS"
  --concurrency 2
)
if [[ -n "$EVAL_WORKERS" ]]; then
  PASSK_ARGS+=(--eval-workers "$EVAL_WORKERS")
fi
npm run hil:swe:passk -- "${PASSK_ARGS[@]}"

echo ""
echo "Recomputing pass@1/pass@3 and deterministic process metrics from saved artifacts..."
python3 scripts/summarize_passk.py \
  --run-id "$RUN_ID" \
  --samples "${PREPARED_DIR}/samples.csv" \
  --human-kb "${PREPARED_DIR}/kb.json" \
  --k 1 \
  --k 3

npm run process-metrics -- \
  --run-id "$RUN_ID" \
  --human-kb "${RUN_DIR}/human-kb.json"

python3 scripts/hil_swe_report.py \
  --run-id "$RUN_ID" \
  --prepared-dir "$PREPARED_DIR" \
  --k 3 \
  --label "First-${NUM_SAMPLES}"

npm run hil:swe:audit -- \
  --run-id "$RUN_ID" \
  --prepared-dir "$PREPARED_DIR" \
  --baseline-report /mnt/efs/mohamedelfeki/Codes/trust_horizon/hil-bench/results/gpt55_xhigh_first3_ask_human/final_report.md

echo ""
python3 - "$RUN_ID" "$PREPARED_DIR" <<'PY'
import json
import sys
from pathlib import Path

run_id = sys.argv[1]
prepared_dir = Path(sys.argv[2])
run_dir = Path("evals") / run_id
metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
process = json.loads((run_dir / "process_metrics.json").read_text(encoding="utf-8"))
progress = json.loads((run_dir / "generation-progress.json").read_text(encoding="utf-8"))

harnesses = ["claude-code", "codex", "opencode"]

def fmt(value):
    if value is None:
        return "missing"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.4f}"
    if isinstance(value, (list, dict)):
        return json.dumps(value, sort_keys=True)
    return str(value)

def table(title, headers, rows):
    print(f"## {title}")
    widths = [len(str(header)) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))
    def line(row):
        return "| " + " | ".join(str(cell).ljust(widths[index]) for index, cell in enumerate(row)) + " |"
    print(line(headers))
    print("| " + " | ".join("-" * width for width in widths) + " |")
    for row in rows:
        print(line(row))
    print("")

pass_rows = []
for harness in harnesses:
    item = (metrics.get("harnesses") or {}).get(harness, {})
    process_item = (process.get("harnesses") or {}).get(harness, {})
    pass_rows.append([
        harness,
        fmt((item.get("pass_at_k") or {}).get("1")),
        fmt((item.get("pass_at_k") or {}).get("3")),
        fmt((item.get("unbiased_pass_at_k") or {}).get("1")),
        fmt((item.get("unbiased_pass_at_k") or {}).get("3")),
        fmt((item.get("swebench_pro_test_pass_at_k") or {}).get("1")),
        fmt((item.get("swebench_pro_test_pass_at_k") or {}).get("3")),
        fmt((item.get("unbiased_swebench_pro_test_pass_at_k") or {}).get("1")),
        fmt((item.get("unbiased_swebench_pro_test_pass_at_k") or {}).get("3")),
        fmt(item.get("hil_evaluator_coverage")),
        fmt(item.get("missing_hil_aligned_eval_attempts")),
        fmt(item.get("ungrounded_or_underconstrained_test_pass_count")),
        fmt(process_item.get("successful_attempt_count")),
        fmt(process_item.get("successful_task_count")),
        fmt(item.get("missing_eval_attempts")),
    ])

table(
    "Pass Metrics",
    [
        "harness",
        "HiL-SWE outcome pass@1",
        "HiL-SWE outcome pass@3",
        "unbiased HiL-SWE outcome pass@1",
        "unbiased HiL-SWE outcome pass@3",
        "diagnostic test pass@1",
        "diagnostic test pass@3",
        "unbiased diagnostic test pass@1",
        "unbiased diagnostic test pass@3",
        "HiL eval coverage",
        "missing aligned evals",
        "underconstrained diagnostic passes",
        "successful attempts",
        "successful tasks",
        "missing evals",
    ],
    pass_rows,
)

preferred_process_order = [
    "attempt_count",
    "task_count",
    "successful_attempt_count",
    "successful_task_count",
    "clarification_request_count",
    "clarification_requests_per_attempt",
    "clarification_requests_per_task",
    "answered_clarification_count",
    "unknown_clarification_count",
    "question_precision",
    "blocker_recall",
    "ASK_F1",
    "irrelevant_unknown_question_rate",
    "duplicate_question_count",
    "duplicate_question_rate",
    "silent_blocker_count",
    "grounded_pass_count",
    "ungrounded_pass_count",
    "questions_before_first_file_edit",
    "questions_before_first_test",
    "questions_after_failed_tests",
    "questions_after_first_patch_edit",
    "time_event_index_to_first_clarification",
    "average_event_index_of_clarification",
    "approval_permission_request_count",
    "approval_permission_requests_per_attempt",
    "approval_permission_requests_per_task",
    "approval_approved_count",
    "approval_denied_count",
    "approval_fallback_count",
    "approval_registry_grounded_count",
    "approval_unknown_count",
    "human_burden_per_successful_attempt",
    "human_burden_per_successful_task",
]

scalar_keys = []
seen = set()
for key in preferred_process_order:
    if any(key in ((process.get("harnesses") or {}).get(harness, {})) for harness in harnesses):
        scalar_keys.append(key)
        seen.add(key)
for harness in harnesses:
    for key, value in ((process.get("harnesses") or {}).get(harness, {})).items():
        if key not in seen and not isinstance(value, (dict, list)):
            scalar_keys.append(key)
            seen.add(key)

process_rows = []
for key in scalar_keys:
    process_rows.append([key] + [fmt(((process.get("harnesses") or {}).get(harness, {})).get(key)) for harness in harnesses])

table("Process Metrics By Harness", ["metric"] + harnesses, process_rows)

collection_rows = []
for key in ["matched_blocker_ids", "trace_completeness", "top_deterministic_failure_signals"]:
    collection_rows.append([key] + [fmt(((process.get("harnesses") or {}).get(harness, {})).get(key)) for harness in harnesses])

table("Process Metric Collections", ["metric"] + harnesses, collection_rows)

print("## Artifacts")
print(f"- run_id: `{run_id}`")
print(f"- run dir: `{run_dir}`")
print(f"- prepared dir: `{prepared_dir}`")
print(f"- KB snapshot: `{progress.get('human_kb')}`")
print(f"- KB source: `{progress.get('human_kb_source')}`")
print(f"- pass metrics: `{run_dir / 'metrics.json'}`")
print(f"- process metrics: `{run_dir / 'process_metrics.json'}`")
print(f"- report: `{run_dir / 'hil_swe_report.md'}`")
PY
