"""HiL-Bench metrics, pooled like run_hil_bench.py (summarize_rows).

rows: dicts with task, mode, pass_num, solve (0/1), n_questions, blockers_resolved, n_blockers.
pass@k: fraction of tasks solved within their first k passes (tasks with >= k valid passes).
Ask P/R/F1 (ask_human mode only): sum(resolved)/sum(questions), sum(resolved)/sum(blockers).
Rows with solve None (environment errors) are excluded. Rows with verified False (agent failures such as
context overflow, counted as unsolved) enter pass@k but not the Ask / reward sums, which need the
verifier's question log.
"""
from __future__ import annotations

from collections import defaultdict


def summarize(rows: list[dict], k_max: int = 3) -> dict[str, dict]:
    by_mode: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r.get("solve") is None:  # infra error -> excluded, as in the official script
            continue
        by_mode[r["mode"]][r["task"]].append(r)
    out = {}
    for mode, tasks in by_mode.items():
        m: dict = {"n_tasks": len(tasks), "n_passes": sum(len(v) for v in tasks.values())}
        for k in range(1, k_max + 1):
            elig = [sorted(v, key=lambda x: x["pass_num"]) for v in tasks.values() if len(v) >= k]
            m[f"pass_at_{k}"] = (sum(any(p["solve"] for p in v[:k]) for v in elig) / len(elig)) if elig else None
        all_p = [p for v in tasks.values() for p in v if p.get("verified", True)]
        m["n_agent_failures"] = m["n_passes"] - len(all_p)
        q = sum(p.get("n_questions") or 0 for p in all_p)
        res = sum(p.get("blockers_resolved") or 0 for p in all_p)
        nb = sum(p.get("n_blockers") or 0 for p in all_p)
        m["avg_questions"] = q / len(all_p) if all_p else 0.0
        m["mean_reward"] = sum(p.get("reward") or 0 for p in all_p) / len(all_p) if all_p else 0.0
        if mode == "ask_human":
            prec = res / q if q else 0.0
            rec = res / nb if nb else 0.0
            m.update(ask_precision=prec, ask_recall=rec,
                     ask_f1=(2 * prec * rec / (prec + rec)) if (prec + rec) else 0.0)
        out[mode] = m
    return out
