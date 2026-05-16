#!/usr/bin/env python3
"""Aggregate skill7 ablation results into a comparable table.

Inputs: runs/_swe_skill7_{base,A,AB,ABD}/<uid>/ask_human/pass_*/result.json
        + stats.json + eval_result.json (if eval ran)

Outputs:
  - prints a Markdown table comparing P/R/F1/pass@k for each config × SDK
  - writes smoke_logs/skill7_ablation_summary.{md,json}
"""
from __future__ import annotations
import json
import pathlib
import sys

CONFIGS = ["base", "A", "AB", "ABD"]
UIDS = ["69c0ead7ef94e54e9dc6a130", "698139c7dc5e90df07566a6c"]
PASSES = 3
RUNS = pathlib.Path("runs")


def _safe(p: pathlib.Path) -> dict:
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _detect_sdk(attempt: dict) -> str | None:
    harness = attempt.get("harness", "")
    if "codex" in harness:
        return "codex"
    if "claude" in harness:
        return "claude"
    return None


def aggregate():
    rows = []
    for cfg in CONFIGS:
        for sdk in ("claude", "codex"):
            agg = {
                "cfg": cfg,
                "sdk": sdk,
                "n_passes_valid": 0,
                "n_passes_resolved": 0,
                "total_questions": 0,
                "total_blockers_present": 0,
                "total_blockers_resolved": 0,
                "silent_resolved": 0,
                "per_uid": {},
            }
            for uid in UIDS:
                # New scheme: SDK-suffixed run-ids prevent cross-SDK clobber.
                run_id = f"_swe_skill7_{cfg}_{sdk}"
                for pi in range(1, PASSES + 1):
                    pdir = RUNS / run_id / uid / "ask_human" / f"pass_{pi}"
                    attempt = _safe(pdir / "attempt.json")
                    if _detect_sdk(attempt) != sdk:
                        continue
                    stats = _safe(pdir / "stats.json")
                    evalr = _safe(pdir / "eval_result.json")
                    result = _safe(pdir / "result.json")
                    if not stats:
                        continue
                    agg["n_passes_valid"] += 1
                    nq = int(stats.get("num_questions") or 0)
                    nbr = int(stats.get("num_blockers_resolved") or 0)
                    nbt = int(stats.get("num_blockers_total") or 0)
                    resolved = bool(evalr.get("resolved") or result.get("resolved"))
                    agg["total_questions"] += nq
                    agg["total_blockers_present"] += nbt
                    agg["total_blockers_resolved"] += nbr
                    if resolved:
                        agg["n_passes_resolved"] += 1
                        if nq == 0:
                            agg["silent_resolved"] += 1
                    per = agg["per_uid"].setdefault(uid, {"q": 0, "br": 0, "bt": 0, "res": 0, "n": 0})
                    per["q"] += nq
                    per["br"] += nbr
                    per["bt"] += nbt
                    per["res"] += int(resolved)
                    per["n"] += 1
            q = max(agg["total_questions"], 0)
            br = agg["total_blockers_resolved"]
            bp = agg["total_blockers_present"]
            agg["precision"] = (br / q) if q else 0.0
            agg["recall"] = (br / bp) if bp else 0.0
            agg["f1"] = (
                2 * agg["precision"] * agg["recall"] / (agg["precision"] + agg["recall"])
                if (agg["precision"] + agg["recall"])
                else 0.0
            )
            agg["pass_rate"] = (
                agg["n_passes_resolved"] / agg["n_passes_valid"]
                if agg["n_passes_valid"]
                else 0.0
            )
            agg["gated_pass_rate"] = (
                (agg["n_passes_resolved"] - agg["silent_resolved"]) / agg["n_passes_valid"]
                if agg["n_passes_valid"]
                else 0.0
            )
            agg["avg_q_per_pass"] = (
                q / agg["n_passes_valid"] if agg["n_passes_valid"] else 0.0
            )
            rows.append(agg)
    return rows


def render(rows: list[dict]) -> str:
    out = []
    out.append("| cfg | sdk | n | pass | gated_pass | avg_q/pass | P | R | F1 |")
    out.append("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        out.append(
            f"| {r['cfg']} | {r['sdk']} | {r['n_passes_valid']} | "
            f"{r['pass_rate']:.2f} | {r['gated_pass_rate']:.2f} | "
            f"{r['avg_q_per_pass']:.2f} | {r['precision']:.2f} | "
            f"{r['recall']:.2f} | {r['f1']:.2f} |"
        )
    return "\n".join(out)


def main():
    rows = aggregate()
    md = render(rows)
    print(md)
    pathlib.Path("smoke_logs/skill7_ablation_summary.md").write_text(md + "\n")
    pathlib.Path("smoke_logs/skill7_ablation_summary.json").write_text(
        json.dumps(rows, indent=2)
    )


if __name__ == "__main__":
    main()
