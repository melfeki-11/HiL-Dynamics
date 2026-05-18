#!/usr/bin/env python3
"""Hard pass/fail: Skill10 Claude/Codex × custom/native vs Alina P/R bars."""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Higher P from guidance, higher R from custom (Skill9 acceptance style).
BARS = {
    "claude": {"P": 0.65, "R": 0.37},
    "codex": {"P": 0.74, "R": 0.65},
}

RUNS = {
    ("custom", "claude"): "_swe_skill10_custom_claude",
    ("custom", "codex"): "_swe_skill10_custom_codex",
    ("native", "claude"): "_swe_skill10_native_claude",
    ("native", "codex"): "_swe_skill10_native_codex",
}

BREADTH = {
    "adk": "_swe_skill10_native_adk",
    "opencode": "_swe_skill10_native_opencode",
}


def _metrics(run_id: str) -> dict | None:
    p = Path("runs") / run_id / "metrics" / "summary.json"
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    for _k, m in data.get("by_mode_agent_model", {}).items():
        if str(_k).startswith("ask_human/"):
            return m
    return None


def main() -> int:
    ok = True
    print("=== Skill10 acceptance (Claude/Codex — hard bar) ===")
    for (arm, sdk), rid in RUNS.items():
        m = _metrics(rid)
        if not m:
            print(f"FAIL missing metrics: {rid}")
            ok = False
            continue
        pr, rr = float(m["ask_precision"]), float(m["ask_recall"])
        bar = BARS[sdk]
        pass_ = pr >= bar["P"] and rr >= bar["R"]
        print(
            f"{'PASS' if pass_ else 'FAIL'} {rid}: P={pr:.3f} R={rr:.3f} "
            f"(need P>={bar['P']} R>={bar['R']})"
        )
        ok = ok and pass_

    print("\n=== Skill10 breadth (ADK/OpenCode — report only) ===")
    for sdk, rid in BREADTH.items():
        m = _metrics(rid)
        if not m:
            print(f"  {sdk}: (no metrics yet) {rid}")
            continue
        print(
            f"  {sdk}: P={m['ask_precision']:.3f} R={m['ask_recall']:.3f} "
            f"avg_q={m.get('avg_questions_per_pass', 'N/A')}"
        )

    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
