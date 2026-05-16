#!/usr/bin/env bash
# Skill7 ablation driver — 4 configs × 2 SDKs × 2 UIDs × 3 passes.
#
# Each config maps to one run-id under runs/. The matrix follows the plan:
#
#   baseline   (Alina's PR, no recall tweaks)
#   A          SEED_BLOCKER_TODOS=1
#   AB         SEED_BLOCKER_TODOS=1, CLAUDE_MD_HINT=1
#   ABD        SEED_BLOCKER_TODOS=1, CLAUDE_MD_HINT=1,
#              RICH_ASK_TOOL_DESC=1, WITH_CUSTOM_TOOL=1
#
# Each invocation runs 6 attempts (2 UIDs × 3 passes) with --workers 6.
# Total: 8 invocations, runs in series so docker / litellm capacity is bounded.

set -euo pipefail

cd /mnt/efs/weijunluo/trust_horizon
mkdir -p smoke_logs

UIDS=(69c0ead7ef94e54e9dc6a130 698139c7dc5e90df07566a6c)
PASSES=3
WORKERS=6

run_config() {
  local run_id="$1"
  local sdk="$2"
  shift 2
  local extra_flags=("$@")
  local log="smoke_logs/${run_id}.${sdk}.log"
  echo "=== [$(date +%H:%M:%S)] $run_id ($sdk) starting ===" | tee -a smoke_logs/skill7_ablation.master.log
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
    "${extra_flags[@]}" \
    > "$log" 2>&1
  echo "=== [$(date +%H:%M:%S)] $run_id ($sdk) done (exit=$?) ===" | tee -a smoke_logs/skill7_ablation.master.log
  tail -n 5 "$log" | tee -a smoke_logs/skill7_ablation.master.log
}

for sdk in claude codex; do
  # baseline — Alina's PR, no recall tweaks
  run_config "_swe_skill7_base"  "$sdk"

  # +A — TodoWriteTool / blocker-checklist seed
  run_config "_swe_skill7_A"     "$sdk" --env SEED_BLOCKER_TODOS=1

  # +A+B — also write CLAUDE.md / AGENTS.md per-task hint
  run_config "_swe_skill7_AB"    "$sdk" --env SEED_BLOCKER_TODOS=1 CLAUDE_MD_HINT=1

  # +A+B+D — also use custom MCP ask_human tool with rich Skill5 description
  run_config "_swe_skill7_ABD"   "$sdk" --with-custom-tool \
    --env SEED_BLOCKER_TODOS=1 CLAUDE_MD_HINT=1 RICH_ASK_TOOL_DESC=1
done

echo "=== [$(date +%H:%M:%S)] ablation finished ==="
