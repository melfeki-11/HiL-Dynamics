#!/usr/bin/env python3
"""Check skill9 full-scale metrics vs both Alina baselines."""
from __future__ import annotations
import json
import sys
from pathlib import Path

ALINA = {
    "claude": {"custom": (0.58, 0.37), "guidance": (0.65, 0.35)},
    "codex": {"custom": (0.56, 0.65), "guidance": (0.74, 0.42)},
}
RUNS = {
    "claude": "_swe_skill9_full_claude",
    "codex": "_swe_skill9_full_codex",
}


def main() -> int:
    ok_all = True
    for sdk, run_id in RUNS.items():
        p = Path("runs") / run_id / "metrics" / "summary.json"
        if not p.exists():
            print(f"FAIL: missing {p}")
            return 1
        data = json.loads(p.read_text())
        m = None
        for _k, v in data.get("by_mode_agent_model", {}).items():
            if str(_k).startswith("ask_human/"):
                m = v
        if not m:
            print(f"FAIL: no metrics for {sdk}")
            return 1
        pr, rr = m["ask_precision"], m["ask_recall"]
        ac, rc = ALINA[sdk]["custom"]
        ag, rg = ALINA[sdk]["guidance"]
        beats_c = pr >= ac and rr >= rc
        beats_g = pr >= ag and rr >= rg
        beats_both = beats_c and beats_g
        print(
            f"{sdk}: P={pr:.3f} R={rr:.3f} | custom={beats_c} guidance={beats_g} BOTH={beats_both}"
        )
        ok_all = ok_all and beats_both
    return 0 if ok_all else 2


if __name__ == "__main__":
    sys.exit(main())
