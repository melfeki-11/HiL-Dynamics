#!/usr/bin/env bash
# Serve a policy for eval.py with the same engine settings as training. Served names:
# "policy" (base model) and, with an adapter, "trained" (base + LoRA).
#   bash rlvr/serve_policy.sh Qwen/Qwen3.5-2B [runs/<run>/exports/global_step_<N>/policy] [port]
set -euo pipefail
source "$(dirname "$0")/env.sh"
BASE_MODEL="$1"; ADAPTER="${2:-}"; PORT="${3:-8011}"
# first-use kernel JIT (FlashInfer GDN prefill, LoRA Triton) can exceed vLLM's 300 s default on a busy host
export VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS="${VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS:-1800}"
ARGS=(--served-model-name policy --port "$PORT" --host 127.0.0.1 --max-model-len 32768
      --gpu-memory-utilization "${RLVR_POLICY_GPU_UTIL:-0.85}" --enforce-eager --trust-remote-code
      --language-model-only --generation-config vllm --mm-processor-cache-gb 0)
if [ -n "$ADAPTER" ]; then
  # vLLM silently ignores LoRA tensors it cannot name-match; exported adapters of multimodal checkpoints
  # (Qwen3.5, Gemma-4) need the language_model. layout (vllm_adapter.py).
  VA="$ADAPTER.vllm"
  "$RLVR_PY" "$(dirname "$0")/vllm_adapter.py" "$BASE_MODEL" "$ADAPTER" "$VA"
  ARGS+=(--enable-lora --max-lora-rank "${RLVR_LORA_RANK:-32}" --lora-modules "trained=$VA")
fi
exec "$RLVR_VENV/bin/vllm" serve "$BASE_MODEL" "${ARGS[@]}"
