#!/usr/bin/env bash
# Re-run Skill10 native arms only (after ablation updates smoke_logs/skill10_native_winner.env).
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/skill1011_common.sh
source scripts/skill1011_common.sh
mkdir -p smoke_logs
skill1011_load_uids

PASSES=3
WORKERS="${SKILL1011_FULL_WORKERS:-5}"
ENV_ABD=($(skill1011_hil_env_abd))
NATIVE_EXTRA=($(skill1011_native_profile_env))

run_one() {
  local sdk="$1"
  local run_id="_swe_skill10_native_${sdk}"
  local log="smoke_logs/${run_id}.log"
  local -a reasoning=($(skill1011_reasoning_for_sdk "$sdk"))
  echo "=== [$(date +%H:%M:%S)] $run_id profile=$(grep NATIVE_PROFILE smoke_logs/skill10_native_winner.env) ===" \
    | tee -a smoke_logs/skill1011_full_scale.master.log
  python3 scripts/run_hil_swe.py \
    --run-id "$run_id" \
    --uids "${UIDS[@]}" \
    --sdk "$sdk" \
    --modes ask_human \
    --passes "$PASSES" \
    --workers "$WORKERS" \
    --force \
    --max-turns 200 \
    "${reasoning[@]}" \
    --env "${ENV_ABD[@]}" "${NATIVE_EXTRA[@]}" \
    > "$log" 2>&1
  python3 scripts/metrics_hil_swe.py --run-id "$run_id" --passes "$PASSES" --print
}

run_one claude
run_one codex
run_one adk
run_one opencode
