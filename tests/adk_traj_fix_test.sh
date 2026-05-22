#!/bin/bash
# Quick test for ADK trajectory fix
# Run: bash tests/adk_traj_fix_test.sh

set -e
TASK_UID="69bc1094b455a91fa20fb868"
HARNESS_IMAGE="hilbench-swe-harness-adk:${TASK_UID}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TASK_DIR="${ROOT_DIR}/data/hil_bench_swe/tasks/${TASK_UID}"
TEST_OUTPUT="${ROOT_DIR}/runs/adk_traj_fix_test"
mkdir -p "$TEST_OUTPUT"
rm -f "$TEST_OUTPUT"/*.json "$TEST_OUTPUT"/*.diff

echo "Running ADK harness (10 steps, ask_human mode)..."
docker run --rm \
  -e MODE=ask_human \
  -e PASS_INDEX=1 \
  -e RUN_ID=adk_traj_fix_test \
  -e MAX_STEPS=10 \
  -e ATTEMPT_TIMEOUT_MS=300000 \
  -e ADK_MODEL="gemini/gemini-3.1-pro" \
  -e LITELLM_BASE_URL="${LITELLM_BASE_URL}" \
  -e LITELLM_API_KEY="${LITELLM_API_KEY}" \
  -e ASK_HUMAN_MODEL="test-judge-model" \
  -e TASK_DIR=/task \
  -e OUTPUT_DIR=/output \
  -e ADK_SUPPRESS_GEMINI_LITELLM_WARNINGS=true \
  -e GIT_PAGER=cat \
  -e PAGER=cat \
  --add-host=host.docker.internal:host-gateway \
  -v "${TASK_DIR}:/task:ro" \
  -v "${ROOT_DIR}/src:/opt/trust_horizon/src:ro" \
  -v "${TEST_OUTPUT}:/output" \
  "$HARNESS_IMAGE" \
  python3.adk /opt/trust_horizon/src/hil_swe/run_adk.py 2>&1
EXIT=$?
echo "Docker exited with: $EXIT"

echo ""
echo "=== result.json ==="
cat "$TEST_OUTPUT/result.json" 2>/dev/null || echo "MISSING"
echo ""
echo "=== stats.json ==="
cat "$TEST_OUTPUT/stats.json" 2>/dev/null || echo "MISSING"
echo ""
echo "=== trajectory summary ==="
python3 - "$TEST_OUTPUT" <<'PYEOF'
import json, sys
try:
    steps = json.load(open(f"{sys.argv[1]}/trajectory.json"))
    has_obs = sum(1 for s in steps if s.get("obs","") and "[no obs" not in s.get("obs",""))
    no_obs  = sum(1 for s in steps if not s.get("obs","") or "[no obs" in s.get("obs",""))
    asks = [s for s in steps if s.get("act","").startswith("ask_human")]
    print(f"Total steps={len(steps)}, with_real_obs={has_obs}, no_obs={no_obs}, asks={len(asks)}")
    for i, s in enumerate(steps[:4]):
        print(f"  Step {i+1}: act={s.get('act','')[:60]} | obs={s.get('obs','')[:60]}")
    if asks:
        print("Questions:")
        for s in asks:
            print(f"  Q: {s['act'][11:80]}")
            print(f"  A: {s['obs'][:80]}")
except Exception as e:
    print(f"Error: {e}")
PYEOF
echo ""
echo "DONE"
