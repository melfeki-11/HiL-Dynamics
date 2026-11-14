"""Per-run base port for SkyRL's vLLM servers and router metrics (RLVR_VLLM_START_PORT; train.py sets it).

SkyRL starts every run's inference servers at a fixed VLLM_START_PORT = 8000 and the router's Prometheus
exporter at a fixed default port, probing upward from there. Two runs on one host can still collide (one
trainer syncing its LoRA into the other run's server, or the second router dying with "Address already in
use"). train.py derives a distinct base per run from its first GPU; servers start there and the router's
metrics port at base + 150, inside the run's own 200-port block.
"""
from __future__ import annotations

import os

from _hook import after_import

SETUP = "skyrl.backends.skyrl_train.inference_servers.setup"
ROUTER = "skyrl.backends.skyrl_train.inference_servers.vllm_router"
METRICS_OFFSET = 150


def _base() -> int | None:
    port = os.environ.get("RLVR_VLLM_START_PORT")
    return int(port) if port else None


def _patch_setup(mod) -> None:
    if _base() is not None:
        mod.VLLM_START_PORT = _base()


def _patch_router(mod) -> None:
    cls = getattr(mod, "VLLMRouter", None)
    if _base() is not None and cls is not None:
        cls._DEFAULT_PROMETHEUS_PORT = _base() + METRICS_OFFSET


def install() -> None:
    after_import(SETUP, _patch_setup)
    after_import(ROUTER, _patch_router)
