#!/usr/bin/env bash
# Poll until Skill1011 campaign finishes; refresh CSV + reports; optional gpu cleanup note.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/skill1011_common.sh
source scripts/skill1011_common.sh
LOG=smoke_logs/skill1011_poll.log
exec >>"$LOG" 2>&1

echo "=== poll start $(date -Iseconds) ==="
while pgrep -f "bash scripts/run_skill1011_campaign.sh" >/dev/null 2>&1; do
  docker_n=$(docker ps -q --filter name=th-swe- 2>/dev/null | wc -l)
  echo "$(date -Iseconds) campaign running docker=$docker_n"
  sleep 600
done

echo "=== campaign process ended $(date -Iseconds) ==="
if [[ -f smoke_logs/skill10_ablation.done ]] && [[ -f smoke_logs/skill10_native_winner.env ]]; then
  echo "Native winner: $(cat smoke_logs/skill10_native_winner.env)"
  echo "If winner != native_soft and native arms ran before ablation finished, run: bash scripts/run_skill10_native_only.sh"
fi

echo "=== 20/20 gate (all 12 run_ids) ==="
while IFS= read -r rid; do
  [[ -n "$rid" ]] || continue
  python3 scripts/skill1011_incomplete_uids.py --run-id "$rid" --passes 3 --print
done < <(skill1011_all_run_ids)

python3 scripts/update_skill1011_csv.py --skill10 --skill11 --replace || true
python3 scripts/write_skill1011_reports.py || true
python3 scripts/acceptance_skill10.py || true
touch smoke_logs/skill1011_campaign.complete
echo "=== poll done $(date -Iseconds) ==="
echo "GPU post: stop your vLLM judge and th-swe-* containers when done (see README.md)."
