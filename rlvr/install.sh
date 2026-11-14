#!/usr/bin/env bash
# Install the RLVR stack into $RLVR_WORK (default: rlvr/.work): SkyRL at a pinned commit, with its
# locked dependencies (torch / vLLM / Harbor) in a uv venv. Needs: git, uv, Docker with compose v2,
# an NVIDIA driver. Pinned wheels are CUDA 13; on drivers older than 580 the NVIDIA CUDA 13
# forward-compatibility user libraries are unpacked locally (no root, nothing installed system-wide).
#   bash rlvr/install.sh
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
WORK=${RLVR_WORK:-$HERE/.work}
SKYRL_REPO=https://github.com/NovaSky-AI/SkyRL.git
SKYRL_COMMIT=a9564572eb8e91076be43c1bd5e4615e3de17265
COMPAT_DEB=cuda-compat-13-0_580.196.07-1ubuntu1_amd64.deb
COMPAT_URL=https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/$COMPAT_DEB

for c in git uv docker nvidia-smi; do command -v "$c" >/dev/null || { echo "missing: $c" >&2; exit 1; }; done
docker compose version >/dev/null || { echo "missing: docker compose v2" >&2; exit 1; }
mkdir -p "$WORK"

if [ ! -d "$WORK/SkyRL/.git" ]; then
  git clone --filter=blob:none "$SKYRL_REPO" "$WORK/SkyRL"
fi
git -C "$WORK/SkyRL" fetch -q origin "$SKYRL_COMMIT" 2>/dev/null || true
git -C "$WORK/SkyRL" checkout -q "$SKYRL_COMMIT"
# Source patches to the pinned SkyRL (idempotent); each file in rlvr/patches/ explains itself.
for p in "$HERE"/patches/*.patch; do
  if git -C "$WORK/SkyRL" apply --reverse --check "$p" 2>/dev/null; then continue; fi
  git -C "$WORK/SkyRL" apply "$p" && echo "applied $(basename "$p")"
done

# Locked environment: fsdp backend + the Harbor integration (Harbor 0.13.1).
(cd "$WORK/SkyRL" && UV_PROJECT_ENVIRONMENT="$WORK/venv" UV_CACHE_DIR="${UV_CACHE_DIR:-$WORK/uv-cache}" \
   uv sync --frozen --python 3.12 --extra fsdp --extra harbor)

driver=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)
if [ "${driver%%.*}" -lt 580 ] && [ ! -d "$WORK/cuda-compat/usr/local/cuda-13.0/compat" ]; then
  echo "driver $driver < 580: unpacking CUDA 13 forward-compat libraries into $WORK/cuda-compat"
  command -v dpkg-deb >/dev/null || { echo "missing: dpkg-deb (needed to unpack $COMPAT_DEB)" >&2; exit 1; }
  mkdir -p "$WORK/cuda-compat"
  (cd "$WORK/cuda-compat" && curl -fsSLO "$COMPAT_URL" && dpkg-deb -x "$COMPAT_DEB" . && rm -f "$COMPAT_DEB")
fi

source "$HERE/env.sh"
"$RLVR_PY" -c "import torch, vllm, harbor; print('torch', torch.__version__, 'cuda ok:', torch.cuda.is_available(), '| vllm', vllm.__version__, '| harbor ok')"
echo "installed into $WORK. Next: source rlvr/env.sh"
