#!/usr/bin/env python3
"""Append `_swe_skill8` rows to the performance CSV after full-scale metrics exist.

Reads runs/_swe_skill8_full_{claude,codex}/metrics/summary.json."""
from __future__ import annotations
import csv
import json
import pathlib
import sys

RUNS = pathlib.Path("runs")
CSV_PATH = pathlib.Path("Trust Horizon Agent Performance - 20-Attempt Test Set.csv")
ROW_DESC = "Skill8 precision recovery on Skill7 ABD (*_swe_skill8)"
ROW_RATIONALE = (
    "SOFTEN_CATEGORY_MANDATE + optional MAX_ASKS_PER_PASS + IRRELEVANT_COOLDOWN; "
    "see smoke_logs/skill8_ablation_summary.md for 2-UID winner."
)


def _read_summary(run_id: str) -> dict | None:
    p = RUNS / run_id / "metrics" / "summary.json"
    if not p.exists():
        return None
    return json.loads(p.read_text())


def _pick_ask_human(summary: dict) -> dict | None:
    if not summary:
        return None
    by = summary.get("by_mode_agent_model", {})
    for key, m in by.items():
        if key.startswith("ask_human/"):
            return m
    return None


def _fmt(v: float | None) -> str:
    if v is None:
        return "N/A"
    return f"{v:.2f}"


def main() -> None:
    rows_to_add = []
    for sdk_label, run_id in (
        ("claude-code", "_swe_skill8_full_claude"),
        ("codex", "_swe_skill8_full_codex"),
    ):
        s = _read_summary(run_id)
        m = _pick_ask_human(s)
        if not m:
            print(f"WARN: no ask_human metrics for {run_id}", file=sys.stderr)
            continue
        rows_to_add.append([
            ROW_DESC if not rows_to_add else "",
            ROW_RATIONALE if not rows_to_add else "",
            sdk_label,
            _fmt(m.get("pass_at_1")), _fmt(m.get("pass_at_3")),
            _fmt(m.get("ask_precision")), _fmt(m.get("ask_recall")), _fmt(m.get("ask_f1")),
        ])

    if not rows_to_add:
        sys.exit("ERROR: no rows to add — run metrics_hil_swe.py for skill8 full runs.")

    with CSV_PATH.open("a", newline="") as f:
        csv.writer(f).writerows(rows_to_add)

    print(f"Appended {len(rows_to_add)} row(s) to {CSV_PATH}")


if __name__ == "__main__":
    main()
