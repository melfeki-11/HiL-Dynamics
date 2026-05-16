#!/usr/bin/env bash
# Skill7 20-UID scale — winning config (Tweak A+B+D + custom MCP tool).
#
# Runs the 20-UID test set × 3 passes × 2 SDKs with the ABD configuration.
# Output goes to runs/_swe_skill7_full_{claude,codex}/ — separate from the
# 2-UID ablation dirs so both sets remain inspectable.
#
# Estimated wall clock: ~50 min per SDK with --workers 10.

set -euo pipefail
cd /mnt/efs/weijunluo/trust_horizon
mkdir -p smoke_logs

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
WORKERS=10

run_one() {
  local sdk="$1"
  local run_id="_swe_skill7_full_${sdk}"
  local log="smoke_logs/${run_id}.log"
  echo "=== [$(date +%H:%M:%S)] $run_id starting (n_uids=${#UIDS[@]}) ===" \
    | tee -a smoke_logs/skill7_full_scale.master.log
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
    --env SEED_BLOCKER_TODOS=1 CLAUDE_MD_HINT=1 RICH_ASK_TOOL_DESC=1 \
    > "$log" 2>&1
  local exitcode=$?
  set -e
  echo "=== [$(date +%H:%M:%S)] $run_id done (exit=$exitcode) ===" \
    | tee -a smoke_logs/skill7_full_scale.master.log
  tail -n 15 "$log" | tee -a smoke_logs/skill7_full_scale.master.log
}

run_one claude
run_one codex
echo "=== [$(date +%H:%M:%S)] full scale finished ===" \
  | tee -a smoke_logs/skill7_full_scale.master.log
