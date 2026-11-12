# Loaded by every Python process whose PYTHONPATH contains this directory (train.py adds it, and
# Ray workers inherit it). Installs the trainer patches in this directory:
#   server_ports.py    - per-run vLLM server base port (RLVR_VLLM_START_PORT, set by train.py)
#   response_logits.py - optional exact logits-only-for-scored-tokens forward (RLVR_RESPONSE_LOGITS=1)
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _install(name: str) -> None:
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    try:
        __import__(name).install()
    except Exception as exc:  # never break interpreter start
        print(f"[rlvr] {name} install failed: {exc}", flush=True)


if os.environ.get("RLVR_VLLM_START_PORT"):
    _install("server_ports")
if os.environ.get("RLVR_RESPONSE_LOGITS") == "1":
    _install("response_logits")
