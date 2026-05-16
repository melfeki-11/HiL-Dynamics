#!/usr/bin/env python3
"""Append a `_swe_skill7` row to the performance CSV after the 20-UID full scale.

Reads runs/_swe_skill7_full_{claude,codex}/metrics/summary.json and writes
two new rows into "Trust Horizon Agent Performance - 20-Attempt Test Set.csv":

  Description: "Layered tweaks A+B+D on top of Alina's PR (*_swe_skill7)"
  Rationale:   "TodoWriteTool seed + CLAUDE.md per-task hint + rich custom MCP
                ask tool description; 2-UID ablation showed +0.42 R for Claude
                and +0.54 R for Codex over the PR baseline."
  Pass@1 / Pass@3 / P / R / F1 from each summary.json.

Run after scripts/run_skill7_full_scale.sh finishes.
"""
from __future__ import annotations
import csv
import json
import pathlib
import sys

RUNS = pathlib.Path("runs")
CSV_PATH = pathlib.Path("Trust Horizon Agent Performance - 20-Attempt Test Set.csv")
ROW_DESC = "Layered tweaks A+B+D on top of Alina's PR (*_swe_skill7)"
ROW_RATIONALE = (
    "TodoWriteTool seed + CLAUDE.md per-task hint + rich custom MCP ask tool "
    "description. 2-UID ablation showed Claude R: 0.29 -> 0.71 (+0.42), Codex "
    "R: 0.38 -> 0.92 (+0.54) over the PR baseline; Codex pass@1 0.33 -> 0.83."
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


def _fmt(v: float) -> str:
    if v is None:
        return "N/A"
    return f"{v:.2f}"


def main():
    rows_to_add = []
    for sdk_label, run_id in [
        ("claude-code", "_swe_skill7_full_claude"),
        ("codex", "_swe_skill7_full_codex"),
    ]:
        s = _read_summary(run_id)
        m = _pick_ask_human(s)
        if not m:
            print(f"WARN: no ask_human metrics for {run_id}", file=sys.stderr)
            continue
        p1 = m.get("pass_at_1")
        p3 = m.get("pass_at_3")
        P = m.get("ask_precision")
        R = m.get("ask_recall")
        F1 = m.get("ask_f1")
        print(f"  {sdk_label:<11} pass@1={_fmt(p1)} pass@3={_fmt(p3)} P={_fmt(P)} R={_fmt(R)} F1={_fmt(F1)}")
        rows_to_add.append([
            ROW_DESC if not rows_to_add else "",
            ROW_RATIONALE if not rows_to_add else "",
            sdk_label,
            _fmt(p1), _fmt(p3), _fmt(P), _fmt(R), _fmt(F1),
        ])

    if not rows_to_add:
        print("ERROR: no rows to add — check summary.json paths.", file=sys.stderr)
        sys.exit(1)

    with CSV_PATH.open("a", newline="") as f:
        w = csv.writer(f)
        for row in rows_to_add:
            w.writerow(row)
    print(f"Appended {len(rows_to_add)} row(s) to {CSV_PATH}")


if __name__ == "__main__":
    main()
