#!/usr/bin/env bash
# Skill9 20-UID full scale — per-SDK profile from ablation winner.
# Usage: bash scripts/run_skill9_full_scale.sh [split|split_JK|split_HEKJ|split_M]

set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p smoke_logs

PROFILE="${1:-split}"

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

UIDS=(
  69bc1094b455a91fa20fb868 698139c7dc5e90df07566a6c
  69a9e77602049c14d2793bb5 69c60cc7b6a31e9900faa779
  69c6ac9f46a2e65fc3988794 69bcc6360c872b9773cce01d
  69be1b17ed0dad79557a9d20 69a7a4d1617c0b97d4d6aacd
  69c0073e28d67846c637cb7e 69c3c5e0b961752c24493b50
  69c3f3301734592b5a14a3b9 69c0ead7ef94e54e9dc6a130
  69be580e4bde28908b05c56f 69c6079bcb74caaa66c49c87
  69b20af8600119b97e678c5b 69c3277deb9e9972372b30fc
  69c2af94ae34531293e5f7ec 69b3ab1df8d713deb4c0087d
  69c196fa0b42d9b078f32b2e 69b1031f73a8f5979167a774
)
PASSES=3
WORKERS="${SKILL9_FULL_WORKERS:-10}"

run_one() {
  local sdk="$1"
  shift
  local extra=("$@")
  local run_id="_swe_skill9_full_${sdk}"
  local log="smoke_logs/${run_id}.log"
  local custom=()
  if [[ "$sdk" == "claude" ]]; then
    custom=("${CLAUDE_CUSTOM[@]}")
  else
    custom=(--with-custom-tool)
  fi
  echo "=== [$(date +%H:%M:%S)] $run_id profile=$PROFILE ===" | tee -a smoke_logs/skill9_full_scale.master.log
  set +e
  python3 scripts/run_hil_swe.py \
    --run-id "$run_id" \
    --uids "${UIDS[@]}" \
    --sdk "$sdk" \
    --modes ask_human \
    --passes "$PASSES" \
    --workers "$WORKERS" \
    --force \
    --max-turns 200 \
    --no-max-reasoning \
    "${custom[@]}" \
    --env "${ENV_ABD[@]}" "${extra[@]}" \
    > "$log" 2>&1
  local ec=$?
  set -e
  echo "=== done $run_id exit=$ec ===" | tee -a smoke_logs/skill9_full_scale.master.log
  tail -n 12 "$log" | tee -a smoke_logs/skill9_full_scale.master.log
}

run_one codex "${CODEX_ENV[@]}"
run_one claude "${CLAUDE_ENV[@]}"
python3 scripts/metrics_hil_swe.py --run-id "_swe_skill9_full_codex" --passes "$PASSES" --print
python3 scripts/metrics_hil_swe.py --run-id "_swe_skill9_full_claude" --passes "$PASSES" --print
