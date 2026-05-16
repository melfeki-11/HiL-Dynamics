#!/usr/bin/env bash
# Skill8 precision ablation — 2 UIDs × 3 passes × base/H/HE/HEG × Claude + Codex.
# Run-ids are SDK-suffixed to avoid overwriting (_swe_skill8_<cfg>_<sdk>).
#
#   base — Skill7 ABD (existing flags only)
#   H    — + SOFTEN_CATEGORY_MANDATE=1
#   HE   — + MAX_ASKS_PER_PASS=5
#   HEG  — + IRRELEVANT_COOLDOWN=2
#
# Estimated wall clock: tens of minutes with workers=6–10 (needs Docker + GPU judge).

set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p smoke_logs

UIDS=(69c0ead7ef94e54e9dc6a130 698139c7dc5e90df07566a6c)
PASSES=3
WORKERS="${SKILL8_ABLATION_WORKERS:-8}"

ENV_ABD=(
  SEED_BLOCKER_TODOS=1
  CLAUDE_MD_HINT=1
  RICH_ASK_TOOL_DESC=1
)

run_config() {
  local cfg="$1"
  local sdk="$2"
  shift 2
  local extra_ev=("$@")
  local run_id="_swe_skill8_${cfg}_${sdk}"
  local log="smoke_logs/${run_id}.log"
  echo "=== [$(date +%H:%M:%S)] $run_id starting ===" \
    | tee -a smoke_logs/skill8_ablation.master.log
  set +e
  python3 scripts/run_hil_swe.py \
    --run-id "$run_id" \
    --uids "${UIDS[@]}" \
    --sdk "$sdk" \
    --modes ask_human \
    --passes "$PASSES" \
    --workers "$WORKERS" \
    --skip-metrics \
    --force \
    --max-turns 200 \
    --no-max-reasoning \
    --with-custom-tool \
    --env "${ENV_ABD[@]}" "${extra_ev[@]}" \
    > "$log" 2>&1
  local exitcode=$?
  set -e
  echo "=== [$(date +%H:%M:%S)] $run_id done (exit=$exitcode) ===" \
    | tee -a smoke_logs/skill8_ablation.master.log
  tail -n 8 "$log" | tee -a smoke_logs/skill8_ablation.master.log
}

for sdk in claude codex; do
  run_config base "$sdk"
  run_config H    "$sdk" SOFTEN_CATEGORY_MANDATE=1
  run_config HE   "$sdk" SOFTEN_CATEGORY_MANDATE=1 MAX_ASKS_PER_PASS=5
  run_config HEG  "$sdk" SOFTEN_CATEGORY_MANDATE=1 MAX_ASKS_PER_PASS=5 IRRELEVANT_COOLDOWN=2
done

python3 scripts/aggregate_skill8_ablation.py
echo "=== [$(date +%H:%M:%S)] skill8 ablation complete ===" \
  | tee -a smoke_logs/skill8_ablation.master.log
