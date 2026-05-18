#!/usr/bin/env bash
# Skill10 native profile ablation — 2 UIDs × 3 passes × native_soft | native_HE | native_strict × CC.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/skill1011_common.sh
source scripts/skill1011_common.sh
mkdir -p smoke_logs

PASSES=3
WORKERS="${SKILL10_ABLATION_WORKERS:-6}"
ENV_ABD=($(skill1011_hil_env_abd))

run_native_cfg() {
  local cfg="$1"
  local sdk="$2"
  shift 2
  local -a extra=("$@")
  local run_id="_swe_skill10_abl_${cfg}_${sdk}"
  local log="smoke_logs/${run_id}.log"
  local -a reasoning=($(skill1011_reasoning_for_sdk "$sdk"))
  echo "=== [$(date +%H:%M:%S)] $run_id ===" | tee -a smoke_logs/skill10_ablation.master.log
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
    "${reasoning[@]}" \
    --env "${ENV_ABD[@]}" "${extra[@]}" \
    > "$log" 2>&1
  local ec=$?
  set -e
  echo "=== done $run_id exit=$ec ===" | tee -a smoke_logs/skill10_ablation.master.log
}

# native_soft: soften only (no custom MCP)
for sdk in claude codex; do
  extra=(SOFTEN_CATEGORY_MANDATE=1)
  if [[ "$sdk" == "claude" ]]; then
    :
  fi
  run_native_cfg native_soft "$sdk" "${extra[@]}"
done

# native_HE: soften + MAX_ASKS on Claude only
run_native_cfg native_HE claude SOFTEN_CATEGORY_MANDATE=1 MAX_ASKS_PER_PASS=5
run_native_cfg native_HE codex SOFTEN_CATEGORY_MANDATE=1

# native_strict: ABD only (strict seed block in guidance)
run_native_cfg native_strict claude
run_native_cfg native_strict codex

python3 scripts/aggregate_skill10_ablation.py
echo done > smoke_logs/skill10_ablation.done
