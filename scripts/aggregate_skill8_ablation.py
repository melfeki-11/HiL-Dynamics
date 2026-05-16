#!/usr/bin/env python3
"""Aggregate skill8 ablation (2 UID) and print Markdown + optional winner hint.

Reads runs/_swe_skill8_{base,H,HE,HEG}_{claude,codex}/... stats.json (+ eval rows).
Winner heuristic (proxy on n=12 passes/SDK):
  composite = min(P_claude,P_codex) + 0.5 * min(R_claude,R_codex)
  eligible if Claude R>=0.55 AND Codex R>=0.80 (else printed as failing gate).

Outputs:
  smoke_logs/skill8_ablation_summary.{md,json}
"""
from __future__ import annotations
import json
import pathlib

CONFIGS = ["base", "H", "HE", "HEG"]
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
    rows: list[dict] = []
    for cfg in CONFIGS:
        for sdk in ("claude", "codex"):
            agg: dict = {
                "cfg": cfg,
                "sdk": sdk,
                "n_passes_valid": 0,
                "n_passes_resolved": 0,
                "total_questions": 0,
                "total_blockers_present": 0,
                "total_blockers_resolved": 0,
                "silent_resolved": 0,
                "total_capped": 0,
                "total_cooldown_denied": 0,
                "per_uid": {},
            }
            run_id = f"_swe_skill8_{cfg}_{sdk}"
            for uid in UIDS:
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
                    nc = int(stats.get("num_ask_human_capped") or 0)
                    nd = int(stats.get("num_ask_human_cooldown_denied") or 0)
                    resolved = bool(evalr.get("resolved") or result.get("resolved"))
                    agg["total_questions"] += nq
                    agg["total_blockers_present"] += nbt
                    agg["total_blockers_resolved"] += nbr
                    agg["total_capped"] += nc
                    agg["total_cooldown_denied"] += nd
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
                agg["n_passes_resolved"] / agg["n_passes_valid"] if agg["n_passes_valid"] else 0.0
            )
            agg["gated_pass_rate"] = (
                (agg["n_passes_resolved"] - agg["silent_resolved"]) / agg["n_passes_valid"]
                if agg["n_passes_valid"]
                else 0.0
            )
            agg["avg_q_per_pass"] = q / agg["n_passes_valid"] if agg["n_passes_valid"] else 0.0
            rows.append(agg)
    return rows


def _cfg_rows(rows: list[dict]) -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {}
    for r in rows:
        out.setdefault(r["cfg"], {})[r["sdk"]] = r
    return out


def winner_hint(rows: list[dict]) -> str:
    by_cfg = _cfg_rows(rows)
    best: tuple[float, str] | None = None
    failing: list[str] = []

    for cfg in CONFIGS:
        pack = by_cfg.get(cfg)
        if not pack or "claude" not in pack or "codex" not in pack:
            failing.append(f"{cfg}: incomplete rows")
            continue
        pc, rc = pack["claude"]["precision"], pack["claude"]["recall"]
        px, rx = pack["codex"]["precision"], pack["codex"]["recall"]
        if rc < 0.55 or rx < 0.80:
            failing.append(f"{cfg}: gate FAIL (Rc={rc:.2f}, Rx={rx:.2f})")
            continue
        score = min(pc, px) + 0.5 * min(rc, rx)
        if best is None or score > best[0]:
            best = (score, cfg)

    lines = []
    if best:
        lines.append(f"recommended_config={best[1]}  composite={best[0]:.4f}")
    else:
        lines.append("recommended_config=NONE_passing_gate  (implement Tweak F fallback per plan)")
    if failing:
        lines.append("gate_notes: " + "; ".join(failing))
    return "\n".join(lines)


def render(rows: list[dict]) -> str:
    lines = []
    lines.append("| cfg | sdk | n | pass | gated | avg_q | cap | cooldown | P | R | F1 |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in sorted(rows, key=lambda x: (x["cfg"], x["sdk"])):
        nv = int(r["n_passes_valid"])
        lines.append(
            f"| {r['cfg']} | {r['sdk']} | {nv} | "
            f"{r['pass_rate']:.2f} | {r['gated_pass_rate']:.2f} | "
            f"{r['avg_q_per_pass']:.2f} | {r['total_capped']} | {r['total_cooldown_denied']} | "
            f"{r['precision']:.2f} | {r['recall']:.2f} | {r['f1']:.2f} |"
        )
    lines.append("")
    lines.append("```")
    lines.append(winner_hint(rows))
    lines.append("```")
    return "\n".join(lines)


def main() -> None:
    rows = aggregate()
    md = render(rows)
    print(md)
    pathlib.Path("smoke_logs").mkdir(exist_ok=True)
    pathlib.Path("smoke_logs/skill8_ablation_summary.md").write_text(md + "\n")
    pathlib.Path("smoke_logs/skill8_ablation_summary.json").write_text(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
