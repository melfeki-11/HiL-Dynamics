#!/usr/bin/env bash
# Full Skill10+11 campaign: reasoning smoke → native ablation → two SDK waves.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p smoke_logs logs

MASTER="smoke_logs/skill1011_campaign.master.log"
exec > >(tee -a "$MASTER") 2>&1

echo "=== Skill1011 campaign start $(date -Iseconds) ==="

bash scripts/verify_skill1011_images.sh

if [[ ! -f smoke_logs/skill10_reasoning_decision.env ]]; then
  echo "=== Phase: reasoning smoke ==="
  bash scripts/run_skill10_reasoning_smoke.sh
else
  echo "=== Phase: reasoning smoke (skip — decision exists) ==="
  cat smoke_logs/skill10_reasoning_decision.env
fi

if [[ ! -f smoke_logs/skill10_native_winner.env ]]; then
  echo "=== Phase: native ablation ==="
  bash scripts/run_skill10_ablation.sh
else
  echo "=== Phase: native ablation (skip — winner exists) ==="
  cat smoke_logs/skill10_native_winner.env
fi

echo "=== Wave 1: Claude + Codex (Skill10 then Skill11) ==="
export SKILL1011_SDK_GROUP=claude_codex
bash scripts/run_skill10_full_scale.sh
bash scripts/run_skill11_full_scale.sh

echo "=== Wave 2: ADK + OpenCode (Skill10 then Skill11) ==="
export SKILL1011_SDK_GROUP=adk_opencode
bash scripts/run_skill10_full_scale.sh
bash scripts/run_skill11_full_scale.sh

echo "=== Phase: 20/20 gate + metrics + CSV + reports ==="
# shellcheck source=scripts/skill1011_common.sh
source scripts/skill1011_common.sh
while IFS= read -r rid; do
  [[ -n "$rid" ]] || continue
  python3 scripts/skill1011_incomplete_uids.py --run-id "$rid" --passes 3 --print
done < <(skill1011_all_run_ids)

for rid in $(skill1011_all_run_ids); do
  python3 scripts/metrics_hil_swe.py --run-id "$rid" --passes 3 --print || true
done

python3 scripts/update_skill1011_csv.py --skill10 --skill11 --replace
python3 scripts/write_skill1011_reports.py
python3 scripts/acceptance_skill10.py || true

echo "=== Skill1011 campaign done $(date -Iseconds) ==="
