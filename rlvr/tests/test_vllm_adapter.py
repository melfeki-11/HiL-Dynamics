import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
sys.path.insert(0, str(Path(__file__).parents[1] / "site"))
import vllm_adapter as lv  # noqa: E402


def test_text_only_peft_names_get_language_model_prefix():
    k = "base_model.model.model.layers.3.linear_attn.in_proj_qkv.lora_A.weight"
    assert list(lv.vlm_names({k: 1})) == [
        "base_model.model.model.language_model.layers.3.linear_attn.in_proj_qkv.lora_A.weight"]


def test_already_multimodal_names_are_unchanged():
    k = "base_model.model.model.language_model.layers.0.mlp.up_proj.lora_B.weight"
    assert lv.vlm_names({k: 1}) == {k: 1}


def test_server_ports_sets_start_port(monkeypatch):
    import types

    import server_ports
    mod = types.SimpleNamespace(VLLM_START_PORT=8000)
    monkeypatch.setenv("RLVR_VLLM_START_PORT", "9200")
    server_ports._patch_setup(mod)
    assert mod.VLLM_START_PORT == 9200
    router = types.SimpleNamespace(VLLMRouter=type("VLLMRouter", (), {"_DEFAULT_PROMETHEUS_PORT": 29000}))
    server_ports._patch_router(router)
    assert router.VLLMRouter._DEFAULT_PROMETHEUS_PORT == 9350
