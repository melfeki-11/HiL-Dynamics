#!/usr/bin/env bash
# Skill11 smoke: 2 UIDs, 1 pass, portable_native on Claude (lint + job start).
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/skill1011_common.sh
source scripts/skill1011_common.sh
mkdir -p smoke_logs

PASSES=1
WORKERS=2
ENV_ARGS=()
skill1011_assert_no_hil_env "${ENV_ARGS[@]}"

python3 scripts/run_hil_swe.py \
  --run-id _swe_skill11_smoke_claude \
  --uids "${SKILL1011_ABLATION_UIDS[@]}" \
  --sdk claude \
  --modes ask_human \
  --passes "$PASSES" \
  --workers "$WORKERS" \
  --force \
  --max-turns 50 \
  --reasoning-effort xhigh \
  --env "${ENV_ARGS[@]}"

echo "Skill11 smoke OK"
