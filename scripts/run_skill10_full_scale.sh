#!/usr/bin/env bash
# Skill10 20-UID: CC custom+native; ADK/OC native only (HiL stack).
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/skill1011_common.sh
source scripts/skill1011_common.sh
mkdir -p smoke_logs
skill1011_load_uids
export UIDS
export PASSES=3
export WORKERS="${SKILL1011_FULL_WORKERS:-5}"
ENV_ABD=($(skill1011_hil_env_abd))
NATIVE_EXTRA=($(skill1011_native_profile_env))
SDK_GROUP="${SKILL1011_SDK_GROUP:-all}"

run_one() {
  local arm="$1"
  local sdk="$2"
  shift 2
  local -a extra=("$@")

  if ! skill1011_sdk_enabled "$sdk"; then
    return 0
  fi
  if [[ "$arm" == "custom" && ( "$sdk" == "adk" || "$sdk" == "opencode" ) ]]; then
    return 0
  fi

  local -a custom=()
  local run_id="_swe_skill10_${arm}_${sdk}"
  local -a reasoning=($(skill1011_reasoning_for_sdk "$sdk"))

  if [[ "$arm" == "custom" ]]; then
    custom=(--with-custom-tool)
  fi

  bash scripts/skill1011_run_arm.sh "$run_id" "$sdk" \
    "${reasoning[@]}" \
    "${custom[@]}" \
    --env "${ENV_ABD[@]}" "${extra[@]}"
}

echo "=== Skill10 full scale SDK_GROUP=$SDK_GROUP $(date -Iseconds) ===" \
  | tee -a smoke_logs/skill1011_full_scale.master.log

# Custom arms (Skill9 split custom profile)
run_one custom codex SOFTEN_CATEGORY_MANDATE=1
run_one custom claude SOFTEN_CATEGORY_MANDATE=1 MAX_ASKS_PER_PASS=5

# Native arms (ablation winner env)
run_one native claude "${NATIVE_EXTRA[@]}"
run_one native codex "${NATIVE_EXTRA[@]}"
run_one native adk "${NATIVE_EXTRA[@]}"
run_one native opencode "${NATIVE_EXTRA[@]}"
