#!/usr/bin/env bash
# Shared helpers for Skill10 / Skill11 drivers.
set -euo pipefail

SKILL1011_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILL1011_UID_FILE="${SKILL1011_UID_FILE:-data/hil_swe_20_attempt_test_set_uids.txt}"
SKILL1011_ABLATION_UIDS=(69c0ead7ef94e54e9dc6a130 698139c7dc5e90df07566a6c)

skill1011_load_uids() {
  UIDS=()
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    line="$(echo "$line" | xargs)"
    [[ -n "$line" ]] && UIDS+=("$line")
  done < "${SKILL1011_ROOT}/${SKILL1011_UID_FILE}"
  if [[ ${#UIDS[@]} -eq 0 ]]; then
    echo "No UIDs in ${SKILL1011_UID_FILE}" >&2
    exit 1
  fi
}

skill1011_hil_env_abd() {
  printf '%s\n' \
    SEED_BLOCKER_TODOS=1 \
    CLAUDE_MD_HINT=1 \
    RICH_ASK_TOOL_DESC=1
}

skill1011_reasoning_args() {
  # Reads smoke_logs/skill10_reasoning_decision.env (REASONING_MODE=xhigh|no_max)
  local decision="${SKILL1011_ROOT}/smoke_logs/skill10_reasoning_decision.env"
  if [[ -f "$decision" ]]; then
    # shellcheck source=/dev/null
    source "$decision"
  else
    REASONING_MODE="${REASONING_MODE:-xhigh}"
  fi
  if [[ "${REASONING_MODE}" == "no_max" ]]; then
    echo --no-max-reasoning
  else
    echo --reasoning-effort xhigh
  fi
}

skill1011_reasoning_for_sdk() {
  local sdk="$1"
  case "$sdk" in
    claude|codex) skill1011_reasoning_args ;;
    adk|opencode) echo --reasoning-effort high ;;
    *) echo "Unknown sdk $sdk" >&2; exit 1 ;;
  esac
}

skill1011_native_profile_env() {
  # Reads smoke_logs/skill10_native_winner.env (NATIVE_PROFILE=native_soft|...)
  local winner="${SKILL1011_ROOT}/smoke_logs/skill10_native_winner.env"
  local profile="native_soft"
  if [[ -f "$winner" ]]; then
    # shellcheck source=/dev/null
    source "$winner"
    profile="${NATIVE_PROFILE:-native_soft}"
  fi
  case "$profile" in
    native_soft)
      echo SOFTEN_CATEGORY_MANDATE=1
      ;;
    native_HE)
      echo SOFTEN_CATEGORY_MANDATE=1
      echo MAX_ASKS_PER_PASS=5
      ;;
    native_strict)
      ;;
    *)
      echo "Unknown NATIVE_PROFILE=$profile" >&2
      exit 1
      ;;
  esac
}

skill1011_sdk_enabled() {
  local sdk="$1"
  local group="${SKILL1011_SDK_GROUP:-all}"
  case "$group" in
    all) return 0 ;;
    claude_codex)
      [[ "$sdk" == "claude" || "$sdk" == "codex" ]]
      ;;
    adk_opencode)
      [[ "$sdk" == "adk" || "$sdk" == "opencode" ]]
      ;;
    *)
      echo "Unknown SKILL1011_SDK_GROUP=$group (use all|claude_codex|adk_opencode)" >&2
      return 1
      ;;
  esac
}

skill1011_all_run_ids() {
  printf '%s\n' \
    _swe_skill10_custom_claude _swe_skill10_custom_codex \
    _swe_skill10_native_claude _swe_skill10_native_codex \
    _swe_skill10_native_adk _swe_skill10_native_opencode \
    _swe_skill11_portable_custom_claude _swe_skill11_portable_custom_codex \
    _swe_skill11_portable_native_claude _swe_skill11_portable_native_codex \
    _swe_skill11_portable_adk _swe_skill11_portable_opencode
}

skill1011_assert_no_hil_env() {
  local -a env_args=("$@")
  local forbidden=(
    SEED_BLOCKER_TODOS=1
    CLAUDE_MD_HINT=1
    RICH_ASK_TOOL_DESC=1
    SOFTEN_CATEGORY_MANDATE=1
  )
  for item in "${env_args[@]}"; do
    for f in "${forbidden[@]}"; do
      if [[ "$item" == "$f" ]]; then
        echo "Skill11 lint FAIL: forbidden HiL env $item" >&2
        exit 1
      fi
    done
    if [[ "$item" == MAX_ASKS_PER_PASS=* ]]; then
      echo "Skill11 lint FAIL: forbidden HiL env $item" >&2
      exit 1
    fi
  done
}
