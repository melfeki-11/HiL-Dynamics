#!/usr/bin/env python3
"""Pick Skill10 reasoning mode (xhigh vs no_max) from 2-UID custom-MCP smoke runs."""
from __future__ import annotations

import json
from pathlib import Path

UIDS = ["69c0ead7ef94e54e9dc6a130", "698139c7dc5e90df07566a6c"]
MODES = ("xhigh", "no_max")
SDKS = ("claude", "codex")
RUNS = Path("runs")
OUT_ENV = Path("smoke_logs/skill10_reasoning_decision.env")
OUT_MD = Path("smoke_logs/skill10_reasoning_decision.md")

ALINA = {
    "claude": {"P": 0.65, "R": 0.37},
    "codex": {"P": 0.74, "R": 0.65},
}


def _metrics(mode: str, sdk: str) -> tuple[float, float, float]:
    rid = f"_swe_skill10_reasoning_{mode}_{sdk}"
    p = RUNS / rid / "metrics" / "summary.json"
    if not p.exists():
        return 0.0, 0.0, 0.0
    data = json.loads(p.read_text())
    for _k, m in data.get("by_mode_agent_model", {}).items():
        if str(_k).startswith("ask_human/"):
            pr = float(m.get("ask_precision") or 0)
            rr = float(m.get("ask_recall") or 0)
            f1 = float(m.get("ask_f1") or 0)
            return pr, rr, f1
    return 0.0, 0.0, 0.0


def main() -> None:
    lines = ["# Skill10 reasoning smoke decision", ""]
    scores: dict[str, float] = {m: 0.0 for m in MODES}

    for mode in MODES:
        lines.append(f"## Mode: `{mode}`")
        for sdk in SDKS:
            pr, rr, f1 = _metrics(mode, sdk)
            bar = ALINA[sdk]
            beats = pr >= bar["P"] and rr >= bar["R"]
            # Score: F1 sum + bonus if beats Alina bar
            s = f1 + (1.0 if beats else 0.0)
            scores[mode] += s
            lines.append(
                f"- **{sdk}**: P={pr:.3f} R={rr:.3f} F1={f1:.3f} "
                f"beats_alina={beats}"
            )
        lines.append("")

    winner = max(MODES, key=lambda m: scores[m])
    if scores["xhigh"] == scores["no_max"]:
        winner = "xhigh"  # default per Alina template

    lines.append(f"**Winner:** `{winner}` (score xhigh={scores['xhigh']:.3f} no_max={scores['no_max']:.3f})")
    lines.append("")
    lines.append("Full 20-UID Skill10 Claude/Codex runs use this mode.")

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    OUT_ENV.write_text(f'REASONING_MODE="{winner}"\n', encoding="utf-8")
    print(OUT_ENV.read_text(), end="")
    print(f"Wrote {OUT_MD}")


if __name__ == "__main__":
    main()
