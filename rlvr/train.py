#!/usr/bin/env python3
"""HiL-Bench RLVR: LoRA GRPO with SkyRL on Harbor tasks running in local Docker (one GPU node).

    SkyRL trainer -> HarborGenerator -> Harbor Trial (Terminus-2 agent) -> Docker compose task
          ^                                                                     |
          +--------- step-wise token ids / logprobs + RLVR reward (reward.txt) <-+

The reward is written by the task verifier (overlay/rl_reward.py); the ask-human sidecar inside each
task calls the judge (serve_judge.sh, or a LiteLLM proxy; see env.sh). Same trial template as eval.py (harbor_trial.yaml).

  source env.sh
  python train.py --validate-only                 # config + imports, no GPU work
  python train.py --run-name qwen2b-sql           # train on tasks/train, eval on tasks/val
Extra key=value arguments are passed through as SkyRL config overrides (applied last).
"""
from __future__ import annotations

import argparse
import copy
import importlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import harbor_common as hc  # noqa: E402

TRAINING_IMPORTS = (
    "vllm_router.launch_router",
    "skyrl.backends.skyrl_train.inference_servers.setup",
    "skyrl.backends.skyrl_train.workers.fsdp.fsdp_worker",
    "skyrl.train.trainer",
    "examples.train_integrations.harbor.entrypoints.main_harbor",
)
# Forwarded to Ray workers: CUDA/toolchain paths, the judge route read by the ask-human sidecar
# (docker compose interpolates them), and the optional trainer patch switch.
FORWARD_ENV = ("PYTHONPATH", "LD_LIBRARY_PATH", "CUDA_HOME", "PATH", "NVCC_APPEND_FLAGS", "HF_HOME", "HF_HUB_OFFLINE",
               "TMPDIR", "RLVR_METRICS_JSONL", "RLVR_RESPONSE_LOGITS", "RLVR_DOCKER_HOST_IP", "RLVR_VLLM_START_PORT",
               "VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS",
               "ASK_HUMAN_BACKEND", "ASK_HUMAN_MODEL", "VLLM_BASE_URL", "VLLM_MODEL", "VLLM_API_KEY",
               "LITELLM_BASE_URL", "LITELLM_API_KEY", "LITELLM_USER")


def judge_route() -> tuple[dict, bool]:
    """(printable judge description, configured?). Never includes keys."""
    backend = os.environ.get("ASK_HUMAN_BACKEND")
    if backend == "vllm":
        return ({"backend": backend, "base_url": os.environ.get("VLLM_BASE_URL"), "model": os.environ.get("VLLM_MODEL")},
                bool(os.environ.get("VLLM_BASE_URL")))
    if backend == "litellm_proxy":
        ok = all(os.environ.get(k) for k in ("ASK_HUMAN_MODEL", "LITELLM_BASE_URL", "LITELLM_API_KEY"))
        return {"backend": backend, "model": os.environ.get("ASK_HUMAN_MODEL"),
                "api_key_set": bool(os.environ.get("LITELLM_API_KEY"))}, ok
    return {"backend": backend}, False


def overrides(a) -> list[str]:
    g = a.gpus
    o = [
        f"data.train_data=['{a.train_dir}']",
        f"data.val_data=['{a.val_dir}']",
        f"trainer.policy.model.path={a.model_path}",
        f"generator.inference_engine.served_model_name={a.served_model_name}",
        "trainer.strategy=fsdp",
        "trainer.algorithm.advantage_estimator=grpo",
        "trainer.algorithm.loss_reduction=token_mean",
        "trainer.algorithm.grpo_norm_by_std=false",
        "trainer.algorithm.use_kl_loss=false",
        f"trainer.algorithm.max_seq_len={a.max_seq_len}",
        # colocated (default): vLLM sleeps while the policy trains on the same GPUs. --inference-gpus N puts vLLM on N
        # separate GPUs instead (no sleep / wake; weights still synced every step, so training stays on-policy).
        f"trainer.placement.colocate_all={'false' if a.inference_gpus else 'true'}",
        "trainer.placement.policy_num_nodes=1", "trainer.placement.ref_num_nodes=1",
        f"trainer.placement.policy_num_gpus_per_node={g}", f"trainer.placement.ref_num_gpus_per_node={g}",
        f"generator.inference_engine.num_engines={(a.inference_gpus or g) // a.tp}",
        f"generator.inference_engine.tensor_parallel_size={a.tp}",
        "generator.inference_engine.backend=vllm",
        "generator.inference_engine.run_engines_locally=true",
        "generator.inference_engine.weight_sync_backend=nccl",
        "generator.inference_engine.enforce_eager=true",
        f"generator.inference_engine.gpu_memory_utilization={a.gpu_memory_utilization}",
        # Qwen3.5's linear-attention layers need one Mamba cache block per sequence slot; vLLM refuses to start when
        # max_num_seqs exceeds the blocks that fit (SkyRL's default 1024 does at low gpu_memory_utilization).
        f"generator.inference_engine.max_num_seqs={a.max_num_seqs}",
        f"generator.inference_engine.engine_init_kwargs.max_model_len={a.max_seq_len}",
        "generator.inference_engine.engine_init_kwargs.enable_log_requests=false",
        "generator.batched=false",
        "generator.step_wise_trajectories=true",
        "generator.merge_stepwise_output=true",
        "generator.apply_overlong_filtering=true",
        f"generator.n_samples_per_prompt={a.n_samples}",
        f"generator.eval_n_samples_per_prompt={a.eval_samples}",
        "generator.rate_limit.enabled=true",
        f"generator.rate_limit.max_concurrency={a.max_concurrency}",
        "generator.rate_limit.trajectories_per_second=2",
        f"trainer.train_batch_size={a.batch}", f"trainer.policy_mini_batch_size={a.batch}",
        "trainer.micro_forward_batch_size_per_gpu=1", "trainer.micro_train_batch_size_per_gpu=1",
        "trainer.update_epochs_per_batch=1",
        f"trainer.max_training_steps={a.max_steps}", "trainer.epochs=1000",
        "trainer.eval_before_train=true", f"trainer.eval_interval={a.eval_interval}",
        f"trainer.eval_batch_size={a.eval_batch}",
        f"trainer.ckpt_interval={a.ckpt_interval}", f"trainer.hf_save_interval={a.hf_save_interval}",
        "trainer.resume_mode=null",
        f"trainer.policy.optimizer_config.lr={a.lr}",
        f"trainer.logger={a.logger}",
        "trainer.project_name=hilbench-rlvr", f"trainer.run_name={a.run_name}",
        f"trainer.ckpt_path={a.artifact_dir}/ckpts", f"trainer.export_path={a.artifact_dir}/exports",
        f"trainer.log_path={a.artifact_dir}/logs",
        "trainer.remove_microbatch_padding=false",
    ]
    if a.lora_rank > 0:
        o += [f"trainer.policy.model.lora.rank={a.lora_rank}", f"trainer.policy.model.lora.alpha={a.lora_alpha}",
              # per-run adapter hand-off dir (SkyRL's default /tmp/skyrl_lora_sync is shared by every run on the host)
              f"trainer.policy.model.lora.lora_sync_path={a.artifact_dir}/lora_sync"]
    if a.language_model_only:  # Qwen3.5 checkpoints are VLMs: train / serve the text tower only
        o += ["trainer.policy.language_model_only=true", "trainer.ref.language_model_only=true",
              "generator.inference_engine.language_model_only=true"]
    return o


def build_cfg(a, extra):
    from examples.train_integrations.harbor.entrypoints.main_harbor import HarborSkyRLConfig, _deep_merge
    from skyrl.train.utils import validate_cfg

    t = hc.training_template(hc.load_template(a.template), max_generate_length=a.max_generate_length,
                             max_seq_len=a.max_seq_len, disable_thinking=a.disable_thinking)
    t["trials_dir"] = f"{a.artifact_dir}/harbor-trials"
    cfg = HarborSkyRLConfig.from_cli_overrides(overrides(a) + extra)
    cfg.harbor_trial_config = _deep_merge(t, cfg.harbor_trial_config)
    validate_cfg(cfg)
    return cfg


def generator_template(cfg) -> dict:
    """The per-trial template SkyRL's HarborGenerator will actually use (for the fingerprint)."""
    from examples.train_integrations.harbor.harbor_generator import HarborGenerator

    class _Endpoint:
        def get_endpoint_url(self):
            return "http://127.0.0.1:8000"

    gen = HarborGenerator(generator_cfg=cfg.generator, harbor_cfg=cfg.harbor_trial_config,
                          inference_engine_client=_Endpoint(), tokenizer=None,
                          max_seq_len=cfg.trainer.algorithm.max_seq_len)
    return copy.deepcopy(gen._harbor_trial_config_template)


def _capture_metrics(path):
    """Append every logged metrics dict to a JSONL file (works with any trainer.logger)."""
    if not path:
        return
    from skyrl.train.utils import tracking

    original = tracking.Tracking.log

    def log(self, data, step, commit=False):
        try:
            row = {"step": step}
            for k, v in data.items():
                try:
                    row[k] = float(v)
                except (TypeError, ValueError):
                    pass
            with open(path, "a") as fh:
                fh.write(json.dumps(row) + "\n")
        except Exception as exc:
            print(f"metrics jsonl capture failed: {exc}", file=sys.stderr)
        return original(self, data, step, commit=commit)

    tracking.Tracking.log = log


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--validate-only", action="store_true")
    p.add_argument("--template", default=str(hc.TEMPLATE))
    p.add_argument("--train-dir", default=str(HERE / "tasks" / "train"))
    p.add_argument("--val-dir", default=str(HERE / "tasks" / "val"))
    p.add_argument("--model-path", default="Qwen/Qwen3.5-2B")
    p.add_argument("--served-model-name", default="policy")
    p.add_argument("--run-name", default="qwen3.5-2b-rlvr")
    p.add_argument("--artifact-dir", default=None, help="default: runs/<run-name>")
    p.add_argument("--gpus", type=int, default=1, help="policy (training) GPUs")
    p.add_argument("--inference-gpus", type=int, default=0, help="separate vLLM GPUs (0 = colocate with the policy)")
    p.add_argument("--tp", type=int, default=1, help="vLLM tensor parallel size; engines = gpus // tp")
    p.add_argument("--batch", type=int, default=4, help="prompts (tasks) per GRPO step")
    p.add_argument("--n-samples", type=int, default=8, help="GRPO group size")
    p.add_argument("--eval-batch", type=int, default=8)
    p.add_argument("--eval-samples", type=int, default=1)
    p.add_argument("--eval-interval", type=int, default=10)
    p.add_argument("--max-steps", type=int, default=50)
    p.add_argument("--max-seq-len", type=int, default=32768)
    p.add_argument("--max-generate-length", type=int, default=2048)
    p.add_argument("--max-concurrency", type=int, default=16, help="concurrent Docker trials")
    p.add_argument("--gpu-memory-utilization", type=float, default=0.5, help="policy vLLM share of each GPU")
    p.add_argument("--lr", default="5.0e-5")
    p.add_argument("--max-num-seqs", type=int, default=256, help="vLLM sequence slots (>= concurrent trajectories)")
    p.add_argument("--lora-rank", type=int, default=32)
    p.add_argument("--lora-alpha", type=int, default=32)
    p.add_argument("--ckpt-interval", type=int, default=0, help="full trainer checkpoints (0 = off)")
    p.add_argument("--hf-save-interval", type=int, default=10, help="LoRA adapter exports for eval")
    p.add_argument("--logger", default="console", help="SkyRL trainer.logger, e.g. console or wandb")
    p.add_argument("--language-model-only", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--disable-thinking", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--response-logits-only", action=argparse.BooleanOptionalAction, default=True,
                   help="trainer forward computes logits only for scored tokens (exact; site/response_logits.py)")
    a, extra = p.parse_known_args()
    a.artifact_dir = str(Path(a.artifact_dir or HERE / "runs" / a.run_name).resolve())
    Path(a.artifact_dir).mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("RLVR_METRICS_JSONL", f"{a.artifact_dir}/metrics.jsonl")
    if a.response_logits_only:
        os.environ["RLVR_RESPONSE_LOGITS"] = "1"
    # distinct vLLM server ports per run, so concurrent runs on one host never share a server (site/server_ports.py)
    first_gpu = (os.environ.get("CUDA_VISIBLE_DEVICES") or "0").split(",")[0]
    os.environ.setdefault("RLVR_VLLM_START_PORT", str(8000 + 200 * int(first_gpu) if first_gpu.isdigit() else 8000))
    # first use of a new shape JIT-compiles kernels (FlashInfer GDN prefill, LoRA Triton); on a busy host that can
    # exceed vLLM's 300 s execute timeout, which kills the engine mid-generation
    os.environ.setdefault("VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS", "1800")
    site = str(HERE / "site")  # sitecustomize: installs the trainer patch in every Ray worker
    if site not in os.environ.get("PYTHONPATH", "").split(os.pathsep):
        os.environ["PYTHONPATH"] = os.pathsep.join(x for x in (site, os.environ.get("PYTHONPATH")) if x)

    cfg = build_cfg(a, extra)
    template = generator_template(cfg)
    for module in TRAINING_IMPORTS:
        importlib.import_module(module)
    judge, judge_ok = judge_route()
    summary = {
        "validated": True,
        "agent_fingerprint": hc.agent_fingerprint(template),
        "agent_model_name": template["agent"]["model_name"],
        "collect_rollout_details": template["agent"]["kwargs"]["collect_rollout_details"],
        "step_wise": cfg.generator.step_wise_trajectories,
        "lora_rank": cfg.trainer.policy.model.lora.rank,
        "trajectories_per_step": cfg.trainer.train_batch_size * cfg.generator.n_samples_per_prompt,
        "judge": judge,
    }
    print(json.dumps(summary, sort_keys=True))
    Path(a.artifact_dir, "config_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    if a.validate_only:
        return
    if not judge_ok:
        raise SystemExit("judge not configured: see env.sh (vllm: start serve_judge.sh; litellm_proxy: export ASK_HUMAN_MODEL, "
                         "LITELLM_BASE_URL, LITELLM_API_KEY)")

    import ray
    import skyrl.train.utils.utils as ray_utils
    from examples.train_integrations.harbor.entrypoints.main_harbor import HarborExp

    original_prepare = ray_utils.prepare_runtime_environment

    def prepare(c):
        env = original_prepare(c)
        env.update({k: os.environ[k] for k in FORWARD_ENV if os.environ.get(k)})
        return env

    ray_utils.prepare_runtime_environment = prepare

    @ray.remote(num_cpus=1)
    def entrypoint(c):
        _capture_metrics(os.environ.get("RLVR_METRICS_JSONL"))
        HarborExp(c).run()

    ray_utils.initialize_ray(cfg)
    ray.get(entrypoint.remote(cfg))


if __name__ == "__main__":
    main()
