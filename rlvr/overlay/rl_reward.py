#!/usr/bin/env python3
"""HiL-Bench RLVR reward (paper, App. H), computed inside the task container after the official verifier.

Reads
  /logs/verifier/reward.json             official verifier output (solve)
  /harbor_shared/ask_human_metrics.json  ask-human sidecar log (ask_human mode only)
  /tests/rl_config.json                  {"lambda_solve": float}
Writes
  /logs/verifier/reward.txt              R, the scalar the trainer optimizes
  /logs/verifier/reward.json             official fields + Ask-F1 metrics + reward decomposition

Per question, in the order asked:
  +0.3  the question discovers a blocker for the first time
  -0.1  irrelevant question, or a repeat of an already-discovered blocker
        (including the sidecar's forced "irrelevant" answer after the per-blocker cap)
   0    judge failure ("can't answer ..."), counted in rl_n_judge_fail, never rewarded or penalized
Terminal: d / |B| if d >= 1 else 0, where d = number of distinct blockers discovered.

  R = 0.3 * (new blockers hit) - 0.1 * (irrelevant + duplicate questions) + (d / |B| if d >= 1) + lambda * solve

lambda = 1 by default; lambda = 0 gives the pure shaped Ask-F1 reward.
Stdlib only: runs with the task image's own Python.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

STEP_HIT, STEP_MISS = 0.3, -0.1
CANT_ANSWER_PREFIX = "can't answer"


def shaped_reward(metrics: dict | None, solve: float, lambda_solve: float = 1.0) -> dict:
    questions = (metrics or {}).get("questions") or []
    n_blockers = int((metrics or {}).get("n_blockers") or 0)
    seen: set[str] = set()
    step_sum, n_hit, n_miss, n_judge_fail = 0.0, 0, 0, 0
    for q in questions:
        name = q.get("blocker_name")
        resp = str(q.get("response") or "")
        if name is None and resp.startswith(CANT_ANSWER_PREFIX):
            n_judge_fail += 1
            continue
        if name is not None and name not in seen:
            seen.add(name)
            step_sum += STEP_HIT
            n_hit += 1
        else:
            step_sum += STEP_MISS
            n_miss += 1
    terminal = (len(seen) / n_blockers) if (n_blockers > 0 and seen) else 0.0
    total = step_sum + terminal + lambda_solve * float(solve)
    # Flat numeric values only: Harbor's VerifierResult rejects nested or null values in reward.json.
    return {
        "reward": round(total, 6),
        "rl_step_sum": round(step_sum, 6),
        "rl_terminal": round(terminal, 6),
        "rl_solve_term": lambda_solve * float(solve),
        "rl_lambda_solve": lambda_solve,
        "rl_n_hits": n_hit,
        "rl_n_misses": n_miss,
        "rl_n_judge_fail": n_judge_fail,
        "rl_blockers_discovered": len(seen),
        "rl_n_blockers": n_blockers,
    }


def main(verifier_dir: str = "/logs/verifier", shared: str = "/harbor_shared", tests: str = "/tests") -> None:
    vdir = Path(verifier_dir)
    official = json.loads((vdir / "reward.json").read_text()) if (vdir / "reward.json").exists() else {}
    solve = float(official.get("solve", official.get("resolved", 0)) or 0)
    cfg_path = Path(tests) / "rl_config.json"
    cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    mpath = Path(shared) / "ask_human_metrics.json"
    metrics = json.loads(mpath.read_text()) if mpath.exists() else None
    out = shaped_reward(metrics, solve, float(cfg.get("lambda_solve", 1.0)))
    merged = {**official, "solve": solve, "official_reward": official.get("reward", solve), **out}
    if metrics is not None:  # official Ask-F1 fields (the SWE verifier does not write them itself)
        for k_src, k_dst in (("total_questions", "n_questions"), ("n_blockers", "n_blockers"),
                             ("blockers_resolved", "blockers_resolved"), ("precision", "precision"),
                             ("recall", "recall"), ("f1", "f1")):
            if metrics.get(k_src) is not None:
                merged.setdefault(k_dst, metrics[k_src])
    merged = {k: v for k, v in merged.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "reward.json").write_text(json.dumps(merged, indent=2))
    (vdir / "reward.txt").write_text(str(out["reward"]))
    print(json.dumps(out))


if __name__ == "__main__":
    main(*sys.argv[1:])
