#!/usr/bin/env python3
"""Aggregate skill9 ablation with Pareto check vs both Alina baselines."""
from __future__ import annotations
import json
import pathlib

UIDS = ["69c0ead7ef94e54e9dc6a130", "698139c7dc5e90df07566a6c"]
PASSES = 3
RUNS = pathlib.Path("runs")

ALINA = {
    "claude": {
        "custom": {"P": 0.58, "R": 0.37},
        "guidance": {"P": 0.65, "R": 0.35},
    },
    "codex": {
        "custom": {"P": 0.56, "R": 0.65},
        "guidance": {"P": 0.74, "R": 0.42},
    },
}


def _safe(p: pathlib.Path) -> dict:
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _detect_sdk(attempt: dict) -> str | None:
    h = attempt.get("harness", "")
    if "codex" in h:
        return "codex"
    if "claude" in h:
        return "claude"
    return None


def aggregate_cfg(cfg: str) -> list[dict]:
    rows = []
    for sdk in ("claude", "codex"):
        agg = {
            "cfg": cfg,
            "sdk": sdk,
            "n_passes_valid": 0,
            "total_questions": 0,
            "total_blockers_present": 0,
            "total_blockers_resolved": 0,
            "total_capped": 0,
            "total_cooldown": 0,
            "total_registry_stop": 0,
        }
        run_id = f"_swe_skill9_{cfg}_{sdk}"
        for uid in UIDS:
            for pi in range(1, PASSES + 1):
                pdir = RUNS / run_id / uid / "ask_human" / f"pass_{pi}"
                attempt = _safe(pdir / "attempt.json")
                if _detect_sdk(attempt) != sdk:
                    continue
                stats = _safe(pdir / "stats.json")
                if not stats:
                    continue
                agg["n_passes_valid"] += 1
                agg["total_questions"] += int(stats.get("num_questions") or 0)
                agg["total_blockers_present"] += int(stats.get("num_blockers_total") or 0)
                agg["total_blockers_resolved"] += int(stats.get("num_blockers_resolved") or 0)
                agg["total_capped"] += int(stats.get("num_ask_human_capped") or 0)
                agg["total_cooldown"] += int(stats.get("num_ask_human_cooldown_denied") or 0)
        q = agg["total_questions"]
        br = agg["total_blockers_resolved"]
        bp = agg["total_blockers_present"]
        agg["precision"] = br / q if q else 0.0
        agg["recall"] = br / bp if bp else 0.0
        agg["f1"] = (
            2 * agg["precision"] * agg["recall"] / (agg["precision"] + agg["recall"])
            if (agg["precision"] + agg["recall"])
            else 0.0
        )
        agg["avg_q_per_pass"] = q / agg["n_passes_valid"] if agg["n_passes_valid"] else 0.0
        rows.append(agg)
    return rows


def _cfg_from_run_dir(name: str) -> str | None:
    """_swe_skill9_split_JK_claude -> split_JK"""
    prefix = "_swe_skill9_"
    if not name.startswith(prefix):
        return None
    rest = name[len(prefix) :]
    for suffix in ("_claude", "_codex"):
        if rest.endswith(suffix):
            return rest[: -len(suffix)]
    return None


def beats_both_alina(sdk: str, p: float, r: float) -> dict:
    c = ALINA[sdk]["custom"]
    g = ALINA[sdk]["guidance"]
    return {
        "beats_custom": p >= c["P"] and r >= c["R"],
        "beats_guidance": p >= g["P"] and r >= g["R"],
        "beats_both": p >= c["P"] and r >= c["R"] and p >= g["P"] and r >= g["R"],
    }


def pareto_score(rows: list[dict]) -> tuple[float, str]:
    """Maximize min ratio to guidance targets across both SDKs."""
    score = 0.0
    notes = []
    for r in rows:
        sdk = r["sdk"]
        g = ALINA[sdk]["guidance"]
        p_ratio = r["precision"] / g["P"] if g["P"] else 0
        r_ratio = r["recall"] / g["R"] if g["R"] else 0
        s = min(p_ratio, r_ratio)
        score += s
        b = beats_both_alina(sdk, r["precision"], r["recall"])
        notes.append(
            f"{sdk}: P={r['precision']:.2f} R={r['recall']:.2f} "
            f"custom={b['beats_custom']} guidance={b['beats_guidance']} both={b['beats_both']}"
        )
    return score, "\n".join(notes)


def main() -> None:
    cfgs = sorted(
        {c for p in RUNS.glob("_swe_skill9_*") if (c := _cfg_from_run_dir(p.name))}
    )
    if not cfgs:
        cfgs = ["split", "split_JK", "split_HEKJ", "split_M", "split_JKF"]

    all_rows: list[dict] = []
    best_cfg = None
    best_score = -1.0
    best_notes = ""

    lines = [
        "| cfg | sdk | n | avg_q | cap | cool | P | R | F1 | custom | guidance | both |",
        "|-----|-----|---:|---:|---:|---:|---:|---:|---:|:---:|:---:|:---:|",
    ]

    for cfg in cfgs:
        rows = aggregate_cfg(cfg)
        if not rows:
            continue
        all_rows.extend(rows)
        score, notes = pareto_score(rows)
        if score > best_score:
            best_score = score
            best_cfg = cfg
            best_notes = notes
        for r in rows:
            b = beats_both_alina(r["sdk"], r["precision"], r["recall"])
            lines.append(
                f"| {cfg} | {r['sdk']} | {r['n_passes_valid']} | {r['avg_q_per_pass']:.2f} | "
                f"{r['total_capped']} | {r['total_cooldown']} | {r['precision']:.2f} | "
                f"{r['recall']:.2f} | {r['f1']:.2f} | "
                f"{'Y' if b['beats_custom'] else ''} | {'Y' if b['beats_guidance'] else ''} | "
                f"{'Y' if b['beats_both'] else ''} |"
            )

    md = "\n".join(lines) + "\n\n```\n"
    md += f"recommended_cfg={best_cfg}  pareto_score={best_score:.3f}\n{best_notes}\n```\n"

    pathlib.Path("smoke_logs").mkdir(exist_ok=True)
    pathlib.Path("smoke_logs/skill9_ablation_summary.md").write_text(md)
    pathlib.Path("smoke_logs/skill9_ablation_summary.json").write_text(
        json.dumps(all_rows, indent=2)
    )
    print(md)


if __name__ == "__main__":
    main()
