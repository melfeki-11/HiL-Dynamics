#!/usr/bin/env bash
# Skill11 20-UID portable: CC portable_custom + portable_native; ADK/OC single arm.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/skill1011_common.sh
source scripts/skill1011_common.sh
mkdir -p smoke_logs
skill1011_load_uids
export UIDS
export PASSES=3
export WORKERS="${SKILL1011_FULL_WORKERS:-5}"
ENV_PORTABLE=()
skill1011_assert_no_hil_env "${ENV_PORTABLE[@]}"
SDK_GROUP="${SKILL1011_SDK_GROUP:-all}"

run_one() {
  local arm="$1"
  local sdk="$2"

  if ! skill1011_sdk_enabled "$sdk"; then
    return 0
  fi

  local run_id
  if [[ "$sdk" == "adk" || "$sdk" == "opencode" ]]; then
    run_id="_swe_skill11_portable_${sdk}"
  else
    run_id="_swe_skill11_portable_${arm}_${sdk}"
  fi

  local -a custom=()
  local -a reasoning=($(skill1011_reasoning_for_sdk "$sdk"))

  if [[ "$arm" == "custom" && ( "$sdk" == "claude" || "$sdk" == "codex" ) ]]; then
    custom=(--with-custom-tool)
  fi

  bash scripts/skill1011_run_arm.sh "$run_id" "$sdk" \
    "${reasoning[@]}" \
    "${custom[@]}" \
    --env "${ENV_PORTABLE[@]}"
}

echo "=== Skill11 full scale SDK_GROUP=$SDK_GROUP $(date -Iseconds) ===" \
  | tee -a smoke_logs/skill1011_full_scale.master.log

run_one portable_custom claude
run_one portable_custom codex
run_one portable_native claude
run_one portable_native codex
run_one portable_native adk
run_one portable_native opencode
