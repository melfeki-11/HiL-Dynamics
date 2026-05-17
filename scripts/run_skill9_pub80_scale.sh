#!/usr/bin/env bash
# Skill9 80 remaining public UIDs — split profile (same as 20-UID winner).
# Usage: bash scripts/run_skill9_pub80_scale.sh [split|split_JK|split_HEKJ|split_M]

set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p smoke_logs logs

PROFILE="${1:-split}"
UID_FILE="data/hil_swe_80_remaining_public_uids.txt"
mapfile -t UIDS < <(grep -v '^#' "$UID_FILE" | grep -v '^[[:space:]]*$')
if [[ ${#UIDS[@]} -eq 0 ]]; then
  echo "No UIDs in $UID_FILE" >&2
  exit 1
fi

ENV_ABD=(
  SEED_BLOCKER_TODOS=1
  CLAUDE_MD_HINT=1
  RICH_ASK_TOOL_DESC=1
)

case "$PROFILE" in
  split)
    CODEX_ENV=(SOFTEN_CATEGORY_MANDATE=1)
    CLAUDE_ENV=(SOFTEN_CATEGORY_MANDATE=1 MAX_ASKS_PER_PASS=5)
    CLAUDE_CUSTOM=(--with-custom-tool)
    ;;
  split_JK|split_M)
    CODEX_ENV=(
      SOFTEN_CATEGORY_MANDATE=1
      BLOCKER_SCALED_CAP=1
      IRRELEVANT_FIRST_THROTTLE=1
      STOP_WHEN_BLOCKERS_RESOLVED=1
    )
    CLAUDE_ENV=("${CODEX_ENV[@]}")
    CLAUDE_CUSTOM=(--with-custom-tool)
    ;;
  split_HEKJ)
    CODEX_ENV=(
      SOFTEN_CATEGORY_MANDATE=1
      BLOCKER_SCALED_CAP=1
      IRRELEVANT_FIRST_THROTTLE=1
      IRRELEVANT_COOLDOWN=2
      STOP_WHEN_BLOCKERS_RESOLVED=1
    )
    CLAUDE_ENV=("${CODEX_ENV[@]}")
    CLAUDE_CUSTOM=(--with-custom-tool)
    ;;
  *)
    echo "Unknown profile $PROFILE" >&2
    exit 1
    ;;
esac

if [[ "$PROFILE" == "split_M" ]]; then
  CLAUDE_CUSTOM=()
fi

PASSES=3
WORKERS="${SKILL9_FULL_WORKERS:-10}"
MASTER_LOG="smoke_logs/skill9_pub80_scale.master.log"

run_one() {
  local sdk="$1"
  shift
  local extra=("$@")
  local run_id="_swe_skill9_pub80_${sdk}"
  local log="smoke_logs/${run_id}.log"
  local custom=()
  if [[ "$sdk" == "claude" ]]; then
    custom=("${CLAUDE_CUSTOM[@]}")
  else
    custom=(--with-custom-tool)
  fi
  echo "=== [$(date +%H:%M:%S)] $run_id profile=$PROFILE n_uids=${#UIDS[@]} ===" | tee -a "$MASTER_LOG"
  set +e
  python3 scripts/run_hil_swe.py \
    --run-id "$run_id" \
    --uids "${UIDS[@]}" \
    --sdk "$sdk" \
    --modes ask_human \
    --passes "$PASSES" \
    --workers "$WORKERS" \
    --max-turns 200 \
    --no-max-reasoning \
    "${custom[@]}" \
    --env "${ENV_ABD[@]}" "${extra[@]}" \
    > "$log" 2>&1
  local ec=$?
  set -e
  echo "=== done $run_id exit=$ec ===" | tee -a "$MASTER_LOG"
  tail -n 12 "$log" | tee -a "$MASTER_LOG"
}

run_one codex "${CODEX_ENV[@]}"
run_one claude "${CLAUDE_ENV[@]}"
python3 scripts/metrics_hil_swe.py --run-id "_swe_skill9_pub80_codex" --passes "$PASSES" --print
python3 scripts/metrics_hil_swe.py --run-id "_swe_skill9_pub80_claude" --passes "$PASSES" --print
