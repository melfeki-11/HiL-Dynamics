"""SkyRL HFModelWrapper.forward: compute logits only for the tokens the loss uses (exact).

With step-wise trajectories every agent turn is a training sample whose prompt is the whole
conversation so far, so micro-batches are long (tens of thousands of tokens) while only a few
hundred response tokens are scored. Upstream materializes logits for every position
(seq_len x vocab) and then keeps the last num_actions + 1. Two exact rewrites, valid because
SkyRL lays each sample out as one left-padded block and slices logprobs/entropy from the right:
  1. drop leading columns that are padding for every row of the micro-batch;
  2. pass logits_to_keep = max(num_actions) + 1 to the HF model and left-pad logprobs/entropy back.
Falls back to upstream with sample packing, sequence parallelism or image inputs.
Enabled by RLVR_RESPONSE_LOGITS=1 (train.py default); installed by site/sitecustomize.py.
"""
from __future__ import annotations

import importlib.abc
import importlib.util
import inspect
import sys

TARGET = "skyrl.backends.skyrl_train.workers.model_wrapper"


class _KeepLogits:
    """Stand-in for HFModelWrapper.model during one forward: injects logits_to_keep."""

    def __init__(self, model, keep: int):
        self._model, self._keep = model, keep

    def __call__(self, *args, **kwargs):
        kwargs.setdefault("logits_to_keep", self._keep)
        return self._model(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._model, name)


def _num_actions_max(na) -> int:
    if isinstance(na, int):
        return na
    try:
        return int(max(na))
    except TypeError:
        return int(na)


def _patch(mod) -> None:
    cls = getattr(mod, "HFModelWrapper", None)
    if cls is None or getattr(cls, "_rlvr_response_logits", False):
        return
    import torch.nn.functional as F

    orig_lp = mod.logprobs_from_logits
    orig_ent = mod.chunked_entropy_from_logits
    orig_fwd = cls.forward
    sig = inspect.signature(orig_fwd)

    def logprobs_from_logits(logits, labels, *args, **kwargs):
        k, s = logits.shape[-2], labels.shape[-1]
        if k == s:
            return orig_lp(logits, labels, *args, **kwargs)
        return F.pad(orig_lp(logits, labels[..., -k:], *args, **kwargs), (s - k, 0))

    def chunked_entropy_from_logits(logits, *args, attention_mask=None, **kwargs):
        k = logits.shape[-2]
        if attention_mask is None or attention_mask.shape[-1] == k:
            return orig_ent(logits, *args, attention_mask=attention_mask, **kwargs)
        s = attention_mask.shape[-1]
        return F.pad(orig_ent(logits, *args, attention_mask=attention_mask[..., -k:], **kwargs), (s - k, 0))

    def forward(self, *args, **kwargs):
        b = sig.bind(self, *args, **kwargs)
        b.apply_defaults()
        a = b.arguments
        mask = a.get("attention_mask")
        if (mask is None or getattr(self, "remove_microbatch_padding", False)
                or getattr(self, "sequence_parallel_size", 1) > 1
                or a.get("pixel_values") is not None or a.get("image_grid_thw") is not None):
            return orig_fwd(**a)
        need = _num_actions_max(a["num_actions"]) + 1  # upstream slices [:, -need:-1]: keep >= need columns
        live = mask.any(0).nonzero()
        first = int(live[0]) if len(live) else 0
        first = max(0, min(first, mask.shape[1] - need))
        if first > 0:
            a["sequences"], a["attention_mask"] = a["sequences"][:, first:], mask[:, first:]
            if a.get("mm_token_type_ids") is not None:
                a["mm_token_type_ids"] = a["mm_token_type_ids"][:, first:]
        keep = min(need, a["sequences"].shape[1])
        model = self.model
        self.__dict__["model"] = _KeepLogits(model, keep)  # shadows the registered submodule
        try:
            return orig_fwd(**a)
        finally:
            self.__dict__.pop("model", None)

    mod.logprobs_from_logits = logprobs_from_logits
    mod.chunked_entropy_from_logits = chunked_entropy_from_logits
    cls.forward = forward
    cls._rlvr_response_logits = True
    print("[rlvr] patched SkyRL HFModelWrapper.forward: left-trim padding + response-only logits", flush=True)


def install() -> None:
    if TARGET in sys.modules:
        _patch(sys.modules[TARGET])
        return

    class _LmFinder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name != TARGET:
                return None
            sys.meta_path.remove(self)
            try:
                spec = importlib.util.find_spec(name)
            finally:
                sys.meta_path.insert(0, self)
            if spec is None or spec.loader is None:
                return spec
            loader = spec.loader

            class _Loader(importlib.abc.Loader):
                def create_module(self, s):
                    return loader.create_module(s)

                def exec_module(self, module):
                    loader.exec_module(module)
                    try:
                        _patch(module)
                    except Exception as exc:  # never break imports
                        print(f"[rlvr] skyrl lm patch failed: {exc}", flush=True)

            spec.loader = _Loader()
            return spec

    if not any(type(f).__name__ == "_LmFinder" for f in sys.meta_path):
        sys.meta_path.insert(0, _LmFinder())
