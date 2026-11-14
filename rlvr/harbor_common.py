"""Harbor trial helpers shared by train.py and eval.py.

build_trial_config mirrors SkyRL's HarborGenerator per-trial construction (pinned commit in
install.sh): deepcopy(template); agent.model_name; agent.kwargs.api_base; task.path; session_id.
Training and evaluation therefore run the same agent, turn limit, environment and verifier;
only the model route differs. Stdlib + PyYAML at import time; Harbor is imported lazily.
"""
from __future__ import annotations

import copy
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "harbor_trial.yaml"


def load_template(path: str | Path = TEMPLATE) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text())


def training_template(template: dict[str, Any], *, max_generate_length: int, max_seq_len: int,
                      disable_thinking: bool = True) -> dict[str, Any]:
    """Pin per-turn and context limits to the trainer's generator settings (train == eval budgets)."""
    t = copy.deepcopy(template)
    kw = t["agent"].setdefault("kwargs", {})
    llm = kw.setdefault("llm_kwargs", {})
    llm["max_tokens"] = max_generate_length
    kw.setdefault("model_info", {}).update(max_input_tokens=max_seq_len, max_output_tokens=max_generate_length)
    if disable_thinking:  # Qwen3.5 chat template: answer directly; Terminus-2 parses the reply
        llm.setdefault("extra_body", {}).setdefault("chat_template_kwargs", {})["enable_thinking"] = False
    return t


def build_trial_config(template: dict[str, Any], task_path: str | Path, *, model_name: str | None,
                       api_base: str | None, agent_name: str | None = None) -> dict[str, Any]:
    config = copy.deepcopy(template)
    agent = config.setdefault("agent", {})
    if agent_name:
        agent["name"] = agent_name
    if model_name is not None:
        agent["model_name"] = model_name
    kwargs = agent.setdefault("kwargs", {})
    if api_base is not None:
        kwargs["api_base"] = api_base
    config["task"] = {"path": str(task_path)}
    kwargs["session_id"] = uuid4().hex
    if agent.get("name") in {"oracle", "nop"}:  # non-LLM agents reject LLM kwargs
        agent.pop("model_name", None)
        agent["kwargs"] = {}
    return config


def agent_fingerprint(config: dict[str, Any]) -> dict[str, Any]:
    """Fields that must match between training and evaluation (model route excluded)."""
    agent = config.get("agent", {})
    kw = agent.get("kwargs", {})
    return {
        "agent": agent.get("name"),
        "agent_timeout_sec": agent.get("override_timeout_sec"),
        "max_turns": kw.get("max_turns"),
        "temperature": kw.get("temperature"),
        "enable_summarize": kw.get("enable_summarize"),
        "max_tokens": kw.get("llm_kwargs", {}).get("max_tokens"),
        "max_input_tokens": kw.get("model_info", {}).get("max_input_tokens"),
        "environment": config.get("environment", {}).get("type"),
        "verifier": config.get("verifier"),
    }


async def run_trial(config: dict[str, Any]) -> dict[str, Any]:
    """Run one Harbor trial and summarize it (reward, exception, turns, tokens, trial_dir).

    Harbor writes each trial to <trials_dir>/<trial_name>; callers set trial_name to find it."""
    from harbor.models.trial.config import TrialConfig
    from harbor.trial.trial import Trial

    started = time.monotonic()
    out: dict[str, Any] = {"task": Path(config["task"]["path"]).name}
    try:
        trial = await Trial.create(TrialConfig.model_validate(config))
        res = await trial.run()
    except Exception as exc:  # environment failure
        return out | {"reward": None, "exception_type": type(exc).__name__, "exception": str(exc)[:500],
                      "seconds": round(time.monotonic() - started, 2)}
    exc_info, verifier, agent_result = res.exception_info, res.verifier_result, res.agent_result
    meta = (getattr(agent_result, "metadata", None) or {}) if agent_result else {}
    return out | {
        "reward": float(verifier.rewards["reward"]) if verifier and verifier.rewards else None,
        "exception_type": exc_info.exception_type if exc_info else None,
        "exception": (exc_info.exception_message or "")[:500] if exc_info else None,
        "n_turns": meta.get("n_episodes"),
        "input_tokens": getattr(agent_result, "n_input_tokens", None) if agent_result else None,
        "output_tokens": getattr(agent_result, "n_output_tokens", None) if agent_result else None,
        "seconds": round(time.monotonic() - started, 2),
        "trial_dir": str(Path(config["trials_dir"]) / config["trial_name"]) if config.get("trial_name") else "",
    }
