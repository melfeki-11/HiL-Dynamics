#!/usr/bin/env python3
"""Evaluate a policy on RLVR tasks with the same agent and trial construction used in training.

  python eval.py tasks/val tasks/swe_eval --model hosted_vllm/policy --api-base http://127.0.0.1:8011/v1 -k 3 --out runs/eval/base
  python eval.py tasks/val --agent oracle --out runs/eval/oracle      # environment check, no LLM
Policy served by serve_policy.sh (names "policy" = base, "trained" = base + LoRA adapter).
Writes <out>/trials.jsonl (one row per trial, incl. reward.json) and <out>/summary.json:
pass@1..k, Ask precision / recall / F1, mean questions and mean RLVR reward per mode.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harbor_common as hc  # noqa: E402
from metrics import summarize  # noqa: E402


def task_dirs(paths):
    out = []
    for p in map(Path, paths):
        out += [p] if (p / "instruction.md").is_file() else sorted(d for d in p.iterdir() if (d / "instruction.md").is_file())
    return out


INFRA_TYPES = ("Environment", "AgentSetup", "AddTestsDir", "RewardFileNotFound")
INFRA_MESSAGES = ("Docker compose command failed", "Failed to start tmux session", "failed to send non-blocking keys")


def infra_error(r: dict) -> bool:
    """Environment / harness failures that say nothing about the agent (container start, test upload, tmux,
    missing verifier output). A busy Docker host produces these in bursts, for whichever model starts first."""
    exc = r.get("exception_type") or ""
    return (not exc or exc.startswith(INFRA_TYPES)
            or (exc == "RuntimeError" and any(m in str(r.get("exception") or "") for m in INFRA_MESSAGES)))


def solve_of(r: dict, rj: dict):
    """Verified trials: the official solve. Agent-side failures (context overflow, agent timeout, ...) are
    failed attempts (solve 0). Infrastructure errors are None: excluded from metrics (and re-run by --resume)."""
    if r.get("reward") is not None:
        return rj.get("solve")
    return None if infra_error(r) else 0.0


def mode_of(name: str) -> str:
    return next(m for m in ("ask_human", "full_info", "baseline") if name.endswith(m))


async def run_all(configs, conc):
    sem = asyncio.Semaphore(conc)

    async def one(c):
        async with sem:
            return await hc.run_trial(c)
    return await asyncio.gather(*(one(c) for c in configs))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tasks", nargs="+", help="task dirs or directories of task dirs")
    ap.add_argument("--agent", default="terminus-2", help="terminus-2 (default), or oracle / nop for environment checks")
    ap.add_argument("--model", default="hosted_vllm/policy")
    ap.add_argument("--api-base", default="http://127.0.0.1:8011/v1")
    ap.add_argument("--max-generate-length", type=int, default=2048)   # = train.py
    ap.add_argument("--max-seq-len", type=int, default=32768)          # = train.py
    ap.add_argument("--disable-thinking", action=argparse.BooleanOptionalAction, default=True)  # = train.py
    ap.add_argument("-k", "--passes", type=int, default=1)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--template", default=str(hc.TEMPLATE))
    ap.add_argument("--out", required=True)
    ap.add_argument("--resume", action="store_true",
                    help="keep the valid rows of an existing <out>/trials.jsonl; re-run only infrastructure failures")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    template = hc.training_template(hc.load_template(a.template), max_generate_length=a.max_generate_length,
                                    max_seq_len=a.max_seq_len, disable_thinking=a.disable_thinking)
    template["agent"]["kwargs"]["collect_rollout_details"] = False  # eval needs no token ids
    template["trials_dir"] = str(out / "trials")
    llm = a.agent not in {"oracle", "nop"}
    # trial_name is also the docker compose project name: tag it with the run so concurrent evals never collide
    tag = "".join(ch if ch.isalnum() else "-" for ch in out.name.lower()).strip("-") or "run"
    kept, attempt = {}, 0
    if a.resume and (out / "trials.jsonl").exists():
        prev = [json.loads(l) for l in (out / "trials.jsonl").read_text().splitlines() if l.strip()]
        kept = {(r["task"], r["pass_num"]): r for r in prev if not infra_error(r) or r.get("verified")}
        attempt = 1 + max([int(str(r.get("trial_dir") or "").rsplit("__r", 1)[1]) for r in prev
                           if "__r" in str(r.get("trial_dir") or "")] or [0])
    configs, metas = [], []
    for t in task_dirs(a.tasks):
        for p in range(1, a.passes + 1):
            if (t.name, p) in kept:
                continue
            c = hc.build_trial_config(template, t, model_name=a.model if llm else None,
                                      api_base=a.api_base if llm else None, agent_name=a.agent)
            c["trial_name"] = f"{t.name}__{tag}__p{p}" + (f"__r{attempt}" if attempt else "")
            c["trial_name"] = c["trial_name"][:120]
            configs.append(c)
            metas.append({"task": t.name, "pass_num": p})
    print(json.dumps({"resume_kept": len(kept), "to_run": len(configs)}), flush=True)
    t0 = time.time()
    results = asyncio.run(run_all(configs, a.concurrency))
    rows = list(kept.values())
    for meta, r in zip(metas, results):
        f = Path(r["trial_dir"]) / "verifier" / "reward.json" if r.get("trial_dir") else None
        rj = json.loads(f.read_text()) if f and f.exists() else {}
        row = {**meta, "mode": mode_of(meta["task"]),
               **{k: r.get(k) for k in ("reward", "exception_type", "exception", "n_turns", "seconds", "trial_dir")},
               "solve": solve_of(r, rj),
               "verified": r.get("reward") is not None,
               "n_questions": rj.get("n_questions") or rj.get("rl_n_hits", 0) + rj.get("rl_n_misses", 0),
               "blockers_resolved": rj.get("blockers_resolved") or 0,
               "n_blockers": rj.get("n_blockers") or rj.get("rl_n_blockers") or 0,
               "reward_json": rj}
        rows.append(row)
        print(json.dumps({k: row[k] for k in ("task", "pass_num", "reward", "solve", "n_questions",
                                               "blockers_resolved", "n_turns", "exception_type")}))
    rows.sort(key=lambda r: (r["task"], r["pass_num"]))
    with open(out / "trials.jsonl", "w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    summary = {"agent": a.agent, "model": a.model if llm else None, "passes": a.passes,
               "wall_s": round(time.time() - t0, 1), "exceptions": sum(1 for r in rows if r["exception_type"]),
               "agent_fingerprint": hc.agent_fingerprint(template), "metrics": summarize(rows, a.passes)}
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
