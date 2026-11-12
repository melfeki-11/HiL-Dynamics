# source rlvr/env.sh  - environment for every rlvr/ command (after install.sh).
RLVR_HOME=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)
export RLVR_HOME RLVR_WORK=${RLVR_WORK:-$RLVR_HOME/.work}
export SKYRL_DIR=$RLVR_WORK/SkyRL RLVR_VENV=$RLVR_WORK/venv
export RLVR_PY=$RLVR_VENV/bin/python
export PYTHONPATH=$SKYRL_DIR:$RLVR_HOME${PYTHONPATH:+:$PYTHONPATH}
export HF_HOME=${HF_HOME:-$RLVR_WORK/hf-cache} UV_CACHE_DIR=${UV_CACHE_DIR:-$RLVR_WORK/uv-cache}
export TMPDIR=${TMPDIR:-$RLVR_WORK/tmp} RAY_TMPDIR=${RAY_TMPDIR:-/tmp/ray_$(id -u)}  # Ray sockets need a short path
mkdir -p "$HF_HOME" "$TMPDIR" "$RAY_TMPDIR"
# Docker bridge address of this host: task containers reach a host-served judge as host.docker.internal.
RLVR_DOCKER_HOST_IP=${RLVR_DOCKER_HOST_IP:-$(ip -4 -o addr show docker0 2>/dev/null | awk '{print $4}' | cut -d/ -f1)}
[ -n "$RLVR_DOCKER_HOST_IP" ] && export RLVR_DOCKER_HOST_IP || unset RLVR_DOCKER_HOST_IP
# CUDA 13 forward-compat user libraries (only present when install.sh found a driver < 580).
C=$RLVR_WORK/cuda-compat/usr/local/cuda-13.0/compat
[ -d "$C" ] && export LD_LIBRARY_PATH=$C${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
# Qwen3.5 linear-attention kernels (TileLang) JIT-compile at runtime with the venv's CUDA 13 nvcc.
export CUDA_HOME=$RLVR_VENV/lib/python3.12/site-packages/nvidia/cu13
export PATH=$CUDA_HOME/bin:$RLVR_VENV/bin:$PATH
# The lock pairs CUDA 13.0 runtime headers with nvcc 13.x; allow the newer compiler.
export NVCC_APPEND_FLAGS="-DCCCL_DISABLE_CTK_COMPATIBILITY_CHECK${NVCC_APPEND_FLAGS:+ $NVCC_APPEND_FLAGS}"

# Ask-human judge, read by the task's ask-human sidecar through docker compose. Two backends:
#  vllm (default)  a self-hosted OpenAI-compatible server (serve_judge.sh). host.docker.internal is
#                  mapped to the host by build_tasks.py; VLLM_BASE_URL has no /v1 suffix.
#  litellm_proxy   a LiteLLM proxy. Export before sourcing this file:
#                    ASK_HUMAN_BACKEND=litellm_proxy ASK_HUMAN_MODEL=litellm_proxy/<model>
#                    LITELLM_BASE_URL=<proxy url> LITELLM_API_KEY=<key> [LITELLM_USER=<user>]
#                  Keep the key in your shell or a mode-600 file, never in this repo.
export ASK_HUMAN_BACKEND=${ASK_HUMAN_BACKEND:-vllm}
export RLVR_JUDGE_PORT=${RLVR_JUDGE_PORT:-8808}
if [ "$ASK_HUMAN_BACKEND" = vllm ]; then
  export VLLM_MODEL=${VLLM_MODEL:-casperhansen/llama-3.3-70b-instruct-awq}
  export ASK_HUMAN_MODEL=$VLLM_MODEL
  export VLLM_BASE_URL=${VLLM_BASE_URL:-http://host.docker.internal:$RLVR_JUDGE_PORT}
fi
