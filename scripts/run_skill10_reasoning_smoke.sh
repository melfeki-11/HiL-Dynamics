#!/usr/bin/env bash
# Skill10: 2-UID × Claude+Codex × (xhigh vs --no-max-reasoning), custom MCP, 1 pass.
# Writes smoke_logs/skill10_reasoning_decision.env and .md
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/skill1011_common.sh
source scripts/skill1011_common.sh
mkdir -p smoke_logs

PASSES=1
WORKERS="${SKILL10_REASONING_SMOKE_WORKERS:-4}"
ENV_ABD=($(skill1011_hil_env_abd))
CODEX_EXTRA=(SOFTEN_CATEGORY_MANDATE=1)
CLAUDE_EXTRA=(SOFTEN_CATEGORY_MANDATE=1 MAX_ASKS_PER_PASS=5)

run_smoke() {
  local mode="$1"
  local sdk="$2"
  shift 2
  local -a extra=("$@")
  local run_id="_swe_skill10_reasoning_${mode}_${sdk}"
  local log="smoke_logs/${run_id}.log"
  local -a reasoning=()
  if [[ "$mode" == "xhigh" ]]; then
    reasoning=(--reasoning-effort xhigh)
  else
    reasoning=(--no-max-reasoning)
  fi
  echo "=== [$(date +%H:%M:%S)] $run_id ===" | tee -a smoke_logs/skill10_reasoning_smoke.master.log
  set +e
  python3 scripts/run_hil_swe.py \
    --run-id "$run_id" \
    --uids "${SKILL1011_ABLATION_UIDS[@]}" \
    --sdk "$sdk" \
    --modes ask_human \
    --passes "$PASSES" \
    --workers "$WORKERS" \
    --force \
    --max-turns 200 \
    --with-custom-tool \
    "${reasoning[@]}" \
    --env "${ENV_ABD[@]}" "${extra[@]}" \
    > "$log" 2>&1
  local ec=$?
  set -e
  echo "=== done $run_id exit=$ec ===" | tee -a smoke_logs/skill10_reasoning_smoke.master.log
  python3 scripts/metrics_hil_swe.py --run-id "$run_id" --passes "$PASSES" --print \
    | tee -a smoke_logs/skill10_reasoning_smoke.master.log
}

for mode in xhigh no_max; do
  run_smoke "$mode" codex "${CODEX_EXTRA[@]}"
  run_smoke "$mode" claude "${CLAUDE_EXTRA[@]}"
done

python3 scripts/aggregate_skill10_reasoning_smoke.py
echo done > smoke_logs/skill10_reasoning_smoke.done
