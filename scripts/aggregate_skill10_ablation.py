#!/usr/bin/env python3
"""Pick Skill10 native profile from 2-UID ablation (native_soft / native_HE / native_strict)."""
from __future__ import annotations

import json
from pathlib import Path

UIDS = ["69c0ead7ef94e54e9dc6a130", "698139c7dc5e90df07566a6c"]
PASSES = 3
CFGs = ("native_soft", "native_HE", "native_strict")
SDKS = ("claude", "codex")
RUNS = Path("runs")
OUT_ENV = Path("smoke_logs/skill10_native_winner.env")
OUT_MD = Path("smoke_logs/skill10_native_winner.md")

ALINA = {
    "claude": {"P": 0.65, "R": 0.37},
    "codex": {"P": 0.74, "R": 0.65},
}


def _aggregate(cfg: str, sdk: str) -> dict:
    agg = {"precision": 0.0, "recall": 0.0, "f1": 0.0, "n": 0}
    rid = f"_swe_skill10_abl_{cfg}_{sdk}"
    for uid in UIDS:
        for pi in range(1, PASSES + 1):
            stats_p = RUNS / rid / uid / "ask_human" / f"pass_{pi}" / "stats.json"
            if not stats_p.exists():
                continue
            stats = json.loads(stats_p.read_text())
            q = int(stats.get("num_questions") or 0)
            br = int(stats.get("num_blockers_resolved") or 0)
            bp = int(stats.get("num_blockers_total") or 0)
            agg["n"] += 1
            if q:
                agg["precision"] += min(1.0, br / q)
            if bp:
                agg["recall"] += min(1.0, br / bp)
    n = agg["n"] or 1
    agg["precision"] /= n
    agg["recall"] /= n
    p, r = agg["precision"], agg["recall"]
    agg["f1"] = 2 * p * r / (p + r) if (p + r) else 0.0
    return agg


def main() -> None:
    lines = ["# Skill10 native profile winner (2-UID ablation)", ""]
    best_cfg = "native_soft"
    best_score = -1.0

    for cfg in CFGs:
        lines.append(f"## `{cfg}`")
        score = 0.0
        for sdk in SDKS:
            m = _aggregate(cfg, sdk)
            bar = ALINA[sdk]
            beats = m["precision"] >= bar["P"] and m["recall"] >= bar["R"]
            score += m["f1"] + (1.0 if beats else 0.0)
            lines.append(
                f"- **{sdk}**: P={m['precision']:.3f} R={m['recall']:.3f} "
                f"F1={m['f1']:.3f} beats_alina={beats}"
            )
        lines.append(f"- score={score:.3f}")
        lines.append("")
        if score > best_score:
            best_score = score
            best_cfg = cfg

    lines.append(f"**Winner:** `{best_cfg}` (score={best_score:.3f})")
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    OUT_ENV.write_text(f'NATIVE_PROFILE="{best_cfg}"\n', encoding="utf-8")
    print(OUT_ENV.read_text(), end="")


if __name__ == "__main__":
    main()
