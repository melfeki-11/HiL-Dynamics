#!/usr/bin/env python3
"""Append Skill10 + Skill11 rows (12 agent lines) to performance CSV."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

RUNS = Path("runs")
CSV_DEFAULT = Path("Trust Horizon Agent Performance - 20-Attempt Test Set.csv")

ADK_OC_RATIONALE = (
    "single arm — --with-custom-tool not supported; Skill10 ADK: near-best SEED+SOFTEN via Phase 0b; "
    "CLAUDE_MD/MAX_ASKS deferred. Not full optimization ceiling."
)

ROWS_SPEC: list[tuple[str, str, str, str]] = [
    # (experiment_label, description, rationale, run_id_suffix pattern)
    ("skill10", "Skill10 custom MCP + HiL stack (*_skill10_custom)", "paper macro P/R (unique blocker IDs per pass); split custom; reasoning per skill10_reasoning_decision.env", "custom"),
    ("skill10", "Skill10 native ask + HiL stack (*_skill10_native)", "paper macro P/R (unique blocker IDs); native profile per skill10_native_winner.env", "native"),
    ("skill11", "Skill11 portable + custom MCP (*_skill11_portable_custom)", "paper macro P/R (unique blocker IDs); portable skill only; no HiL env flags", "portable_custom"),
    ("skill11", "Skill11 portable + native ask (*_skill11_portable_native)", "paper macro P/R (unique blocker IDs); primary portable answer; no HiL env flags", "portable_native"),
]

SDK_AGENT = {
    "claude": "claude-code",
    "codex": "codex",
    "adk": "adk",
    "opencode": "opencode",
}


def _read_summary(run_id: str) -> dict | None:
    p = RUNS / run_id / "metrics" / "summary.json"
    return json.loads(p.read_text()) if p.exists() else None


def _pick(summary: dict) -> dict | None:
    for k, m in summary.get("by_mode_agent_model", {}).items():
        if str(k).startswith("ask_human/"):
            return m
    return None


def _fmt(v) -> str:
    return "N/A" if v is None else f"{float(v):.2f}"


def _run_id(exp: str, arm: str, sdk: str) -> str:
    if exp == "skill10":
        if arm in ("custom", "native"):
            if sdk in ("adk", "opencode") and arm == "custom":
                return ""  # no custom arm
            return f"_swe_skill10_{arm}_{sdk}"
    if exp == "skill11":
        if sdk in ("adk", "opencode"):
            return f"_swe_skill11_portable_{sdk}"
        return f"_swe_skill11_{arm}_{sdk}"
    return ""


def build_block(
    exp: str,
    desc: str,
    rationale: str,
    arm: str,
    *,
    allow_partial: bool = False,
) -> list[list[str]]:
    out: list[list[str]] = []
    sdks = ["claude", "codex", "adk", "opencode"]
    for sdk in sdks:
        rid = _run_id(exp, arm, sdk)
        if not rid:
            continue
        summary = _read_summary(rid)
        if not summary:
            print(f"  skip (no summary): {rid}")
            continue
        m = _pick(summary)
        if not m:
            print(f"  skip (no metrics): {rid}")
            continue
        n_attempts = int(m.get("num_attempts") or 0)
        if n_attempts < 20 and not allow_partial:
            print(f"  skip (partial n={n_attempts}/20): {rid}")
            continue
        rat = rationale
        if sdk in ("adk", "opencode") and exp == "skill10":
            rat = ADK_OC_RATIONALE
        if sdk in ("adk", "opencode") and exp == "skill11":
            rat = "single portable arm; identical harness for portable_custom vs portable_native"

        print(
            f"  {rid} n={m.get('num_attempts')} P={_fmt(m.get('ask_precision'))} "
            f"R={_fmt(m.get('ask_recall'))} avg_q={_fmt(m.get('avg_questions_per_pass'))}"
        )
        out.append([
            desc if not out else "",
            rat if not out else "",
            SDK_AGENT.get(sdk, sdk),
            _fmt(m.get("pass_at_1")),
            _fmt(m.get("pass_at_3")),
            _fmt(m.get("ask_precision")),
            _fmt(m.get("ask_recall")),
            _fmt(m.get("ask_f1")),
            _fmt(m.get("avg_questions_per_pass")),
        ])
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", type=Path, default=CSV_DEFAULT)
    parser.add_argument("--skill10", action="store_true")
    parser.add_argument("--skill11", action="store_true")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Remove existing Skill10/11 rows before appending.",
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Include rows with num_attempts < 20 (default: skip partial runs).",
    )
    args = parser.parse_args()
    if not args.skill10 and not args.skill11:
        args.skill10 = args.skill11 = True

    if not args.csv.exists():
        raise SystemExit(f"CSV not found: {args.csv}")

    all_rows: list[list[str]] = []
    if args.skill10:
        for _exp, desc, rat, arm in ROWS_SPEC:
            if _exp != "skill10":
                continue
            all_rows.extend(
                build_block("skill10", desc, rat, arm, allow_partial=args.allow_partial)
            )
    if args.skill11:
        for _exp, desc, rat, arm in ROWS_SPEC:
            if _exp != "skill11":
                continue
            # ADK/OC: only one portable run — use portable_native block only once
            if arm == "portable_custom":
                block = build_block(
                    "skill11", desc, rat, arm, allow_partial=args.allow_partial
                )
                # filter to claude/codex only for custom arm
                all_rows.extend(block)
            else:
                all_rows.extend(
                    build_block("skill11", desc, rat, arm, allow_partial=args.allow_partial)
                )

    if not all_rows:
        raise SystemExit("No rows to append (run metrics first?)")

    if args.replace:
        markers = {desc for _exp, desc, _rat, _arm in ROWS_SPEC}
        kept: list[list[str]] = []
        with args.csv.open(newline="") as f:
            for row in csv.reader(f):
                if row and row[0] in markers:
                    continue
                kept.append(row)
        with args.csv.open("w", newline="") as f:
            csv.writer(f).writerows(kept)
        print(f"Replaced Skill10/11 rows matching: {sorted(markers)}")

    with args.csv.open("a", newline="") as f:
        csv.writer(f).writerows(all_rows)
    print(f"Appended {len(all_rows)} row(s) to {args.csv}")


if __name__ == "__main__":
    main()
