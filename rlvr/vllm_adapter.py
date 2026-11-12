"""Write a vLLM-ready copy of an exported LoRA adapter.

    python rlvr/vllm_adapter.py <base_model> <adapter_dir> <out_dir>

Qwen3.5 / Gemma-4 checkpoints are multimodal; training is language-model-only through the text-only HF
class, so PEFT names the adapter tensors `base_model.model.model.layers.*`. vLLM serves the multimodal
class, whose LoRA loader expects `base_model.model.model.language_model.layers.*` and silently skips
tensors it cannot match (the adapter loads and changes nothing). serve_policy.sh runs this before
serving an exported adapter; for a text-only base model the copy is unchanged. The training-time weight
sync gets the same renaming from patches/skyrl-lora-vlm-names.patch.
"""
from __future__ import annotations

import os
import shutil
import sys

PEFT_PREFIX = "base_model.model.model."
LM = "language_model."


def vlm_names(params: dict) -> dict:
    out = {}
    for k, v in params.items():
        if k.startswith(PEFT_PREFIX) and not k.startswith(PEFT_PREFIX + LM):
            k = PEFT_PREFIX + LM + k[len(PEFT_PREFIX):]
        out[k] = v
    return out


def convert(base_model: str, adapter: str, out: str) -> bool:
    """Copy `adapter` to `out`, renaming tensors for vLLM if `base_model` is multimodal. Returns renamed?"""
    from safetensors.torch import load_file, save_file
    from transformers import AutoConfig

    cfg = AutoConfig.from_pretrained(base_model, trust_remote_code=True)
    multimodal = getattr(cfg, "vision_config", None) is not None
    os.makedirs(out, exist_ok=True)
    for f in os.listdir(adapter):
        if f != "adapter_model.safetensors" and os.path.isfile(os.path.join(adapter, f)):
            shutil.copy(os.path.join(adapter, f), out)
    sd = load_file(os.path.join(adapter, "adapter_model.safetensors"))
    save_file(vlm_names(sd) if multimodal else sd, os.path.join(out, "adapter_model.safetensors"))
    return multimodal


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit("usage: vllm_adapter.py <base_model> <adapter_dir> <out_dir>")
    renamed = convert(*sys.argv[1:])
    print(f"[rlvr] adapter -> {sys.argv[3]} ({'vLLM multimodal names' if renamed else 'unchanged'})")
