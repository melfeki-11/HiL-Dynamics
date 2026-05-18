#!/usr/bin/env python3
"""Generate smoke_logs/skill10_vs_alina.md and skill11_portable.md from metrics."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

RUNS = Path("runs")
OUT10 = Path("smoke_logs/skill10_vs_alina.md")
OUT11 = Path("smoke_logs/skill11_portable.md")

SKILL10_CC = [
    ("custom", "claude", "_swe_skill10_custom_claude"),
    ("custom", "codex", "_swe_skill10_custom_codex"),
    ("native", "claude", "_swe_skill10_native_claude"),
    ("native", "codex", "_swe_skill10_native_codex"),
]
SKILL10_BREADTH = [
    ("adk", "_swe_skill10_native_adk"),
    ("opencode", "_swe_skill10_native_opencode"),
]
SKILL11_PORTABLE_NATIVE = [
    ("claude", "_swe_skill11_portable_native_claude"),
    ("codex", "_swe_skill11_portable_native_codex"),
    ("adk", "_swe_skill11_portable_adk"),
    ("opencode", "_swe_skill11_portable_opencode"),
]
SKILL11_CUSTOM = [
    ("claude", "_swe_skill11_portable_custom_claude"),
    ("codex", "_swe_skill11_portable_custom_codex"),
]

CATEGORY_PHRASES = [
    "Missing parameter values",
    "Unclear return type",
    "Ambiguous spec",
    "Unclear scope",
    "Edge-case behavior",
]


def _m(run_id: str) -> dict | None:
    p = RUNS / run_id / "metrics" / "summary.json"
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    for _k, v in data.get("by_mode_agent_model", {}).items():
        if str(_k).startswith("ask_human/"):
            return v
    return None


def _row(m: dict | None) -> str:
    if not m:
        return "| (pending) | | | | | |"
    return (
        f"| {m.get('num_attempts', 'N/A')} "
        f"| {m.get('pass_at_3', 0):.2f} "
        f"| {m.get('ask_precision', 0):.2f} "
        f"| {m.get('ask_recall', 0):.2f} "
        f"| {m.get('ask_f1', 0):.2f} "
        f"| {m.get('avg_questions_per_pass', 0):.2f} |"
    )


def _grep_templates() -> str:
    root = Path("src/hil_swe/templates")
    lines = ["### Portable skill overindexing check", ""]
    for name in ("ask_human_skill.md", "ask_human_guidance.txt"):
        path = root / name
        hits = []
        text = path.read_text(encoding="utf-8")
        for phrase in CATEGORY_PHRASES:
            if phrase in text:
                hits.append(phrase)
        lines.append(f"- `{path}`: {len(hits)} category phrase hits (expect 0)")
    lines.append("")
    return "\n".join(lines)


def write_skill10() -> None:
    lines = [
        "# Skill10 vs Alina (Aim 1: best metrics via customization)",
        "",
        "Headline: **4 Claude/Codex cells** (custom × native). ADK/OpenCode are breadth only.",
        "",
        "| Arm | SDK | n | pass@3 | P | R | F1 | avg_q |",
        "|-----|-----|---|--------|---|---|----|-------|",
    ]
    for arm, sdk, rid in SKILL10_CC:
        lines.append(f"| {arm} | {sdk} {_row(_m(rid))}".replace("| (pending)", f"| {arm} | {sdk} | (pending)"))
        # fix table - simpler approach
    lines = [
        "# Skill10 vs Alina (Aim 1: best metrics via customization)",
        "",
        "Headline deliverable: Claude/Codex × {custom MCP, native-only} at best HiL config.",
        "",
        "**Metric:** paper macro ask P/R/F1 — mean of per-pass ratios; `num_blockers_resolved` = unique blocker IDs (hil-bench comparable).",
        "",
        "| Arm | SDK | n | pass@3 | P | R | F1 | avg_q |",
        "|-----|-----|---|--------|---|---|----|-------|",
    ]
    for arm, sdk, rid in SKILL10_CC:
        m = _m(rid)
        if m:
            lines.append(
                f"| {arm} | {sdk} | {m.get('num_attempts')} | {m.get('pass_at_3', 0):.2f} | "
                f"{m.get('ask_precision', 0):.2f} | {m.get('ask_recall', 0):.2f} | "
                f"{m.get('ask_f1', 0):.2f} | {m.get('avg_questions_per_pass', 0):.2f} |"
            )
        else:
            lines.append(f"| {arm} | {sdk} | pending | | | | | |")

    lines.extend([
        "",
        "## Breadth (out of Alina quoted scope)",
        "",
        "| SDK | run-id | P | R | F1 | avg_q |",
        "|-----|--------|---|---|----|-------|",
    ])
    for sdk, rid in SKILL10_BREADTH:
        m = _m(rid)
        if m:
            lines.append(
                f"| {sdk} | `{rid}` | {m.get('ask_precision', 0):.2f} | "
                f"{m.get('ask_recall', 0):.2f} | {m.get('ask_f1', 0):.2f} | "
                f"{m.get('avg_questions_per_pass', 0):.2f} |"
            )
        else:
            lines.append(f"| {sdk} | `{rid}` | pending | | | |")

    lines.extend([
        "",
        "Run `python3 scripts/acceptance_skill10.py` for hard pass/fail vs Alina bars.",
        "",
    ])
    OUT10.parent.mkdir(parents=True, exist_ok=True)
    OUT10.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {OUT10}")


def write_skill11() -> None:
    lines = [
        "# Skill11 portable (Aim 2: production harness strategy)",
        "",
        "**Primary portable answer:** `portable_native` (same treatment on all 4 SDKs).",
        "",
        "**Metric:** paper macro ask P/R/F1 — unique blocker IDs per pass (same as Skill10 / hil-bench).",
        "",
        "## portable_native (all SDKs)",
        "",
        "| SDK | run-id | n | pass@3 | P | R | F1 | avg_q |",
        "|-----|--------|---|--------|---|---|----|-------|",
    ]
    for sdk, rid in SKILL11_PORTABLE_NATIVE:
        m = _m(rid)
        if m:
            lines.append(
                f"| {sdk} | `{rid}` | {m.get('num_attempts')} | {m.get('pass_at_3', 0):.2f} | "
                f"{m.get('ask_precision', 0):.2f} | {m.get('ask_recall', 0):.2f} | "
                f"{m.get('ask_f1', 0):.2f} | {m.get('avg_questions_per_pass', 0):.2f} |"
            )
        else:
            lines.append(f"| {sdk} | `{rid}` | pending | | | | | |")

    lines.extend([
        "",
        "## Scaffolding lift (Skill10 native − Skill11 portable_native)",
        "",
        "| SDK | ΔP | ΔR | ΔF1 | Δavg_q |",
        "|-----|----|----|-----|--------|",
    ])
    for sdk, rid11 in SKILL11_PORTABLE_NATIVE:
        rid10 = f"_swe_skill10_native_{sdk}"
        m10, m11 = _m(rid10), _m(rid11)
        if m10 and m11:
            dp = float(m10["ask_precision"]) - float(m11["ask_precision"])
            dr = float(m10["ask_recall"]) - float(m11["ask_recall"])
            df = float(m10["ask_f1"]) - float(m11["ask_f1"])
            dq = float(m10.get("avg_questions_per_pass") or 0) - float(
                m11.get("avg_questions_per_pass") or 0
            )
            lines.append(f"| {sdk} | {dp:+.2f} | {dr:+.2f} | {df:+.2f} | {dq:+.2f} |")
        else:
            lines.append(f"| {sdk} | pending | | | |")

    lines.extend([
        "",
        "## portable_custom (Claude/Codex enrichment only)",
        "",
    ])
    for sdk, rid in SKILL11_CUSTOM:
        m = _m(rid)
        if m:
            lines.append(
                f"- **{sdk}** `{rid}`: P={m.get('ask_precision', 0):.2f} "
                f"R={m.get('ask_recall', 0):.2f} F1={m.get('ask_f1', 0):.2f}"
            )
        else:
            lines.append(f"- **{sdk}** `{rid}`: pending")

    lines.extend(["", _grep_templates()])
    OUT11.parent.mkdir(parents=True, exist_ok=True)
    OUT11.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {OUT11}")


def main() -> None:
    write_skill10()
    write_skill11()
    try:
        subprocess.run(["python3", "scripts/acceptance_skill10.py"], check=False)
    except Exception:
        pass


if __name__ == "__main__":
    main()
