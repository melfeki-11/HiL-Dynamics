#!/usr/bin/env python3
"""Append skill9 full-scale rows to the performance CSV."""
from __future__ import annotations
import csv
import json
from pathlib import Path

RUNS = Path("runs")
CSV_PATH = Path("Trust Horizon Agent Performance - 20-Attempt Test Set.csv")
ROW_DESC = "Skill9 Pareto split profile on Skill7 ABD (*_swe_skill9)"
ROW_RATIONALE = (
    "2-UID ablation winner split: Codex SOFTEN only (H); Claude soften+MAX_ASKS_PER_PASS=5 (HE). "
    "Beats both Alina custom-tool and skill+guidance on 2 UID; see smoke_logs/skill9_ablation_summary.md."
)

ALINA = {
    "claude-code": {"custom": (0.58, 0.37), "guidance": (0.65, 0.35)},
    "codex": {"custom": (0.56, 0.65), "guidance": (0.74, 0.42)},
}


def _read_summary(run_id: str) -> dict | None:
    p = RUNS / run_id / "metrics" / "summary.json"
    return json.loads(p.read_text()) if p.exists() else None


def _pick(summary: dict) -> dict | None:
    for k, m in summary.get("by_mode_agent_model", {}).items():
        if k.startswith("ask_human/"):
            return m
    return None


def _fmt(v) -> str:
    return "N/A" if v is None else f"{v:.2f}"


def main() -> None:
    rows = []
    for label, rid in (("claude-code", "_swe_skill9_full_claude"), ("codex", "_swe_skill9_full_codex")):
        m = _pick(_read_summary(rid))
        if not m:
            print(f"WARN: missing {rid}")
            continue
        p, r = m.get("ask_precision"), m.get("ask_recall")
        rows.append([
            ROW_DESC if not rows else "",
            ROW_RATIONALE if not rows else "",
            label,
            _fmt(m.get("pass_at_1")),
            _fmt(m.get("pass_at_3")),
            _fmt(p),
            _fmt(r),
            _fmt(m.get("ask_f1")),
        ])
        ac = ALINA[label]["custom"]
        ag = ALINA[label]["guidance"]
        both = p >= ac[0] and r >= ac[1] and p >= ag[0] and r >= ag[1]
        print(f"  {label}: P={p:.3f} R={r:.3f} beats_both_alina={both}")

    if not rows:
        raise SystemExit("no rows")
    with CSV_PATH.open("a", newline="") as f:
        csv.writer(f).writerows(rows)
    print(f"Appended to {CSV_PATH}")


if __name__ == "__main__":
    main()
