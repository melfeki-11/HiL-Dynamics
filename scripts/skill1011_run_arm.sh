#!/usr/bin/env bash
# Run one Skill10/11 arm with initial --force and retry until 20/20 metric-eligible UIDs.
#
# Do not skip the initial --force call when resuming; stats_schema_version=2 gate
# ensures retry-only paths cannot leak legacy event-count stats into metrics.
#
# Usage: skill1011_run_arm.sh RUN_ID SDK [run_hil_swe extra args...]
# Required env: UIDS[], PASSES, WORKERS (optional SKILL1011_FULL_WORKERS)
set -euo pipefail

RUN_ID="${1:?run_id required}"
SDK="${2:?sdk required}"
shift 2
EXTRA_ARGS=("$@")

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck source=scripts/skill1011_common.sh
source scripts/skill1011_common.sh
skill1011_load_uids
mkdir -p smoke_logs

PASSES="${PASSES:-3}"
WORKERS="${WORKERS:-${SKILL1011_FULL_WORKERS:-5}}"
MAX_RETRIES="${SKILL1011_MAX_UID_RETRIES:-3}"
MASTER_LOG="${SKILL1011_MASTER_LOG:-smoke_logs/skill1011_full_scale.master.log}"
LOG="smoke_logs/${RUN_ID}.log"

_run_hil_swe() {
  local want_force="$1"
  shift
  local -a batch_uids=("$@")
  local -a force_flag=()
  if [[ "$want_force" == "1" ]]; then
    force_flag=(--force)
  fi
  python3 scripts/run_hil_swe.py \
    --run-id "$RUN_ID" \
    --uids "${batch_uids[@]}" \
    --sdk "$SDK" \
    --modes ask_human \
    --passes "$PASSES" \
    --workers "$WORKERS" \
    "${force_flag[@]}" \
    --max-turns 200 \
    "${EXTRA_ARGS[@]}"
}

echo "=== [$(date +%H:%M:%S)] $RUN_ID sdk=$SDK n_uids=${#UIDS[@]} ===" \
  | tee -a "$MASTER_LOG"

set +e
_run_hil_swe 1 "${UIDS[@]}" > "$LOG" 2>&1
initial_ec=$?
set -e
echo "=== initial $RUN_ID exit=$initial_ec ===" | tee -a "$MASTER_LOG"
tail -n 8 "$LOG" | tee -a "$MASTER_LOG" || true

round=0
while (( round < MAX_RETRIES )); do
  if python3 scripts/skill1011_incomplete_uids.py --run-id "$RUN_ID" --passes "$PASSES" >/dev/null 2>&1; then
    break
  fi
  round=$((round + 1))
  missing_line=$(python3 scripts/skill1011_incomplete_uids.py --run-id "$RUN_ID" --passes "$PASSES" \
    --query incomplete_uids || true)
  read -r -a missing <<< "$missing_line"
  if [[ ${#missing[@]} -eq 0 || -z "${missing[0]:-}" ]]; then
    break
  fi
  retry_log="smoke_logs/${RUN_ID}.retry${round}.log"
  echo "=== retry $round $RUN_ID incomplete=${#missing[@]} ===" | tee -a "$MASTER_LOG"
  set +e
  if (( round == 1 )); then
    _run_hil_swe 0 "${missing[@]}" > "$retry_log" 2>&1
  else
    _run_hil_swe 1 "${missing[@]}" > "$retry_log" 2>&1
  fi
  retry_ec=$?
  set -e
  echo "=== retry $round $RUN_ID exit=$retry_ec ===" | tee -a "$MASTER_LOG"
  tail -n 8 "$retry_log" | tee -a "$MASTER_LOG" || true
done

if ! python3 scripts/skill1011_incomplete_uids.py --run-id "$RUN_ID" --passes "$PASSES" --print; then
  echo "FAIL incomplete: $RUN_ID (not 20/20 after $MAX_RETRIES retries)" | tee -a "$MASTER_LOG"
  exit 1
fi

python3 scripts/metrics_hil_swe.py --run-id "$RUN_ID" --passes "$PASSES" --print \
  | tee -a "$MASTER_LOG"
