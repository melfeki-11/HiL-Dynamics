#!/usr/bin/env python3
"""Append or replace Skill9 performance CSV rows from metrics summary.json."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

RUNS = Path("runs")
CSV_PATH = Path("Trust Horizon Agent Performance - 20-Attempt Test Set.csv")

SLICES = {
    "20": {
        "description": "Skill9 Pareto split profile on Skill7 ABD (*_swe_skill9)",
        "rationale": (
            "split profile on 20-UID test set; Codex soften-only, Claude soften+MAX_ASKS_PER_PASS=5. "
            "avg_q from runs/_swe_skill9_full_*. "
            "P/R: capped-macro proxy (event-count stats capped per pass); upper bound vs paper unique-count ~0.02-0.07."
        ),
        "claude_run": "_swe_skill9_full_claude",
        "codex_run": "_swe_skill9_full_codex",
    },
    "80": {
        "description": "Skill9 split — 80 remaining public UIDs (*_swe_skill9_pub80)",
        "rationale": (
            "Same split profile on public 100 minus 20-UID test set. "
            "avg_q from runs/_swe_skill9_pub80_*. "
            "P/R: capped-macro proxy (event-count stats capped per pass)."
        ),
        "claude_run": "_swe_skill9_pub80_claude",
        "codex_run": "_swe_skill9_pub80_codex",
    },
    "100": {
        "description": "Skill9 split — full 100 public UIDs (*_swe_skill9_pub100)",
        "rationale": (
            "Merged metrics: 20-UID full + 80-UID pub80 (symlink merge). "
            "avg_q from runs/_swe_skill9_pub100_*. "
            "P/R: capped-macro proxy (event-count stats capped per pass)."
        ),
        "claude_run": "_swe_skill9_pub100_claude",
        "codex_run": "_swe_skill9_pub100_codex",
    },
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
    if v is None:
        return "N/A"
    return f"{float(v):.2f}"


def _avg_q(m: dict) -> str:
    return _fmt(m.get("avg_questions_per_pass"))


def build_rows(slice_key: str) -> list[list[str]]:
    cfg = SLICES[slice_key]
    rows: list[list[str]] = []
    for label, rid in (("claude-code", cfg["claude_run"]), ("codex", cfg["codex_run"])):
        summary = _read_summary(rid)
        if not summary:
            raise SystemExit(f"missing summary for {rid}")
        m = _pick(summary)
        if not m:
            raise SystemExit(f"no ask_human metrics in {rid}")
        n = m.get("num_attempts", m.get("pass_at_3_n", "?"))
        print(
            f"  [{slice_key}] {label} n={n} pass@1={_fmt(m.get('pass_at_1'))} "
            f"pass@3={_fmt(m.get('pass_at_3'))} P={_fmt(m.get('ask_precision'))} "
            f"R={_fmt(m.get('ask_recall'))} avg_q={_avg_q(m)}"
        )
        rows.append([
            cfg["description"] if not rows else "",
            cfg["rationale"] if not rows else "",
            label,
            _fmt(m.get("pass_at_1")),
            _fmt(m.get("pass_at_3")),
            _fmt(m.get("ask_precision")),
            _fmt(m.get("ask_recall")),
            _fmt(m.get("ask_f1")),
            _avg_q(m),
        ])
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--slice",
        choices=["20", "80", "100", "all"],
        default="all",
        help="Which UID slice to append (default: all three blocks).",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=CSV_PATH,
        help="Performance CSV path.",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Remove existing rows for the selected slice(s) before appending.",
    )
    args = parser.parse_args()

    if not args.csv.exists():
        raise SystemExit(f"CSV not found: {args.csv}")

    keys = ["20", "80", "100"] if args.slice == "all" else [args.slice]
    all_rows: list[list[str]] = []
    for k in keys:
        all_rows.extend(build_rows(k))

    if args.replace:
        markers = {SLICES[k]["description"] for k in keys}
        kept: list[list[str]] = []
        with args.csv.open(newline="") as f:
            for row in csv.reader(f):
                if row and row[0] in markers:
                    continue
                kept.append(row)
        with args.csv.open("w", newline="") as f:
            csv.writer(f).writerows(kept)
        print(f"Replaced slice row(s) matching: {sorted(markers)}")

    with args.csv.open("a", newline="") as f:
        csv.writer(f).writerows(all_rows)
    print(f"Appended {len(all_rows)} row(s) to {args.csv}")


if __name__ == "__main__":
    main()
