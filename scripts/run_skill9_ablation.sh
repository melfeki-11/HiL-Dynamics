#!/usr/bin/env bash
# Skill9 Pareto ablation — per-SDK env profiles on 2 UIDs × 3 passes.
#
# Profiles (each runs codex + claude with different --env):
#   split       — Codex H only; Claude HE (soften + fixed cap 5)
#   split_JK    — Codex H+J+K+L; Claude soften + J + K (no fixed cap)
#   split_HEKJ  — split_JK + cooldown=2 on both
#   split_M     — split_JK but Claude without --with-custom-tool

set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p smoke_logs

UIDS=(69c0ead7ef94e54e9dc6a130 698139c7dc5e90df07566a6c)
PASSES=3
WORKERS="${SKILL9_ABLATION_WORKERS:-8}"

ENV_ABD=(
  SEED_BLOCKER_TODOS=1
  CLAUDE_MD_HINT=1
  RICH_ASK_TOOL_DESC=1
  ASK_HUMAN_BASE_URL=
)

run_sdk() {
  local cfg="$1"
  local sdk="$2"
  shift 2
  local extra=("$@")
  local run_id="_swe_skill9_${cfg}_${sdk}"
  local log="smoke_logs/${run_id}.log"
  local custom_flag=(--with-custom-tool)
  if [[ "$cfg" == *"_M" ]] && [[ "$sdk" == "claude" ]]; then
    custom_flag=()
  fi
  echo "=== [$(date +%H:%M:%S)] $run_id starting ===" | tee -a smoke_logs/skill9_ablation.master.log
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
    "${custom_flag[@]}" \
    --env "${ENV_ABD[@]}" "${extra[@]}" \
    > "$log" 2>&1
  local ec=$?
  set -e
  echo "=== [$(date +%H:%M:%S)] $run_id done (exit=$ec) ===" | tee -a smoke_logs/skill9_ablation.master.log
  tail -n 6 "$log" | tee -a smoke_logs/skill9_ablation.master.log
}

run_profile() {
  local name="$1"
  local codex_env=($2)
  local claude_env=($3)
  run_sdk "$name" codex "${codex_env[@]}"
  run_sdk "$name" claude "${claude_env[@]}"
}

# Codex H / Claude HE
run_profile split \
  "SOFTEN_CATEGORY_MANDATE=1" \
  "SOFTEN_CATEGORY_MANDATE=1 MAX_ASKS_PER_PASS=5"

# Codex H+J+K+L / Claude J+K (blocker-scaled cap, irrelevant-first)
run_profile split_JK \
  "SOFTEN_CATEGORY_MANDATE=1 BLOCKER_SCALED_CAP=1 IRRELEVANT_FIRST_THROTTLE=1 STOP_WHEN_BLOCKERS_RESOLVED=1" \
  "SOFTEN_CATEGORY_MANDATE=1 BLOCKER_SCALED_CAP=1 IRRELEVANT_FIRST_THROTTLE=1 STOP_WHEN_BLOCKERS_RESOLVED=1"

# + cooldown
run_profile split_HEKJ \
  "SOFTEN_CATEGORY_MANDATE=1 BLOCKER_SCALED_CAP=1 IRRELEVANT_FIRST_THROTTLE=1 IRRELEVANT_COOLDOWN=2 STOP_WHEN_BLOCKERS_RESOLVED=1" \
  "SOFTEN_CATEGORY_MANDATE=1 BLOCKER_SCALED_CAP=1 IRRELEVANT_FIRST_THROTTLE=1 IRRELEVANT_COOLDOWN=2 STOP_WHEN_BLOCKERS_RESOLVED=1"

# Claude native-only variant of JK
run_sdk split_M codex \
  SOFTEN_CATEGORY_MANDATE=1 BLOCKER_SCALED_CAP=1 IRRELEVANT_FIRST_THROTTLE=1 STOP_WHEN_BLOCKERS_RESOLVED=1
run_sdk split_M claude \
  SOFTEN_CATEGORY_MANDATE=1 BLOCKER_SCALED_CAP=1 IRRELEVANT_FIRST_THROTTLE=1 STOP_WHEN_BLOCKERS_RESOLVED=1

# Tweak F — read-before-ask (Claude tracks Read/Grep; Codex env forwarded for bridge parity)
run_profile split_JKF \
  "SOFTEN_CATEGORY_MANDATE=1 BLOCKER_SCALED_CAP=1 IRRELEVANT_FIRST_THROTTLE=1 STOP_WHEN_BLOCKERS_RESOLVED=1 READ_BEFORE_ASK=1" \
  "SOFTEN_CATEGORY_MANDATE=1 BLOCKER_SCALED_CAP=1 IRRELEVANT_FIRST_THROTTLE=1 STOP_WHEN_BLOCKERS_RESOLVED=1 READ_BEFORE_ASK=1"

python3 scripts/aggregate_skill9_ablation.py | tee -a smoke_logs/skill9_ablation.master.log
echo "=== [$(date +%H:%M:%S)] skill9 ablation complete ===" | tee -a smoke_logs/skill9_ablation.master.log
