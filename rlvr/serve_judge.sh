#!/usr/bin/env bash
# Serve the ask-human judge with vLLM (default: the paper's Llama-3.3-70B-Instruct AWQ, ~40 GB, one GPU).
#   CUDA_VISIBLE_DEVICES=1 bash rlvr/serve_judge.sh [extra vllm serve args]
# Binds to the Docker bridge address (reachable from task containers as host.docker.internal);
# override with RLVR_JUDGE_HOST. Model: VLLM_MODEL; memory share: RLVR_JUDGE_GPU_UTIL.
set -euo pipefail
source "$(dirname "$0")/env.sh"
[ "$ASK_HUMAN_BACKEND" = vllm ] || { echo "ASK_HUMAN_BACKEND=$ASK_HUMAN_BACKEND: no local judge to serve" >&2; exit 1; }
HOST=${RLVR_JUDGE_HOST:-$(ip -4 -o addr show docker0 2>/dev/null | awk '{print $4}' | cut -d/ -f1)}
HOST=${HOST:-0.0.0.0}
echo "judge $VLLM_MODEL on http://$HOST:$RLVR_JUDGE_PORT (containers: $VLLM_BASE_URL)"
exec "$RLVR_VENV/bin/vllm" serve "$VLLM_MODEL" --served-model-name "$VLLM_MODEL" \
  --host "$HOST" --port "$RLVR_JUDGE_PORT" --max-model-len "${RLVR_JUDGE_MAX_LEN:-16384}" \
  --gpu-memory-utilization "${RLVR_JUDGE_GPU_UTIL:-0.9}" --generation-config vllm "$@"
