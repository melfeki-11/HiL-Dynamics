#!/usr/bin/env bash
# Skill8 20-UID full scale — pass winning config slug as argv[1]: base | H | HE | HEG (default HEG).

set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p smoke_logs

CFG="${1:-HEG}"

case "$CFG" in
  base)
    SKILL8_EXTRA=()
    ;;
  H)
    SKILL8_EXTRA=( SOFTEN_CATEGORY_MANDATE=1 )
    ;;
  HE)
    SKILL8_EXTRA=( SOFTEN_CATEGORY_MANDATE=1 MAX_ASKS_PER_PASS=5 )
    ;;
  HEG)
    SKILL8_EXTRA=( SOFTEN_CATEGORY_MANDATE=1 MAX_ASKS_PER_PASS=5 IRRELEVANT_COOLDOWN=2 )
    ;;
  *)
    echo "Unknown cfg '$CFG' (use base|H|HE|HEG)" >&2
    exit 1
    ;;
esac

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
WORKERS="${SKILL8_FULL_WORKERS:-10}"

run_one() {
  local sdk="$1"
  local run_id="_swe_skill8_full_${sdk}"
  local log="smoke_logs/${run_id}.log"
  echo "=== [$(date +%H:%M:%S)] $run_id cfg=$CFG starting (n=${#UIDS[@]}) ===" \
    | tee -a smoke_logs/skill8_full_scale.master.log
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
    --with-custom-tool \
    --env SEED_BLOCKER_TODOS=1 CLAUDE_MD_HINT=1 RICH_ASK_TOOL_DESC=1 "${SKILL8_EXTRA[@]}" \
    > "$log" 2>&1
  local exitcode=$?
  set -e
  echo "=== [$(date +%H:%M:%S)] $run_id done (exit=$exitcode) ===" \
    | tee -a smoke_logs/skill8_full_scale.master.log
  tail -n 15 "$log" | tee -a smoke_logs/skill8_full_scale.master.log
}

run_one claude
run_one codex
python3 scripts/metrics_hil_swe.py --run-id "_swe_skill8_full_claude" --passes "$PASSES" >/dev/null
python3 scripts/metrics_hil_swe.py --run-id "_swe_skill8_full_codex" --passes "$PASSES" >/dev/null
echo "=== [$(date +%H:%M:%S)] full scale metrics written (cfg=$CFG) ===" \
  | tee -a smoke_logs/skill8_full_scale.master.log
