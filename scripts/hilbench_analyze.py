"""
hilbench analyze — generate a human-readable report for a completed HiL-Bench run.

Reads runs/<run_id>/metrics/summary.json and pass_level.json (produced by Phase 3
of run_hil_swe.py) and writes:

  runs/<run_id>/report.md       — Markdown scorecard with scorecard table,
                                  ask-behavior summary, and failure examples
  runs/<run_id>/metadata.json   — machine-readable run summary

Usage:
  python3 scripts/hilbench_analyze.py --run-id my-first-run
  python3 scripts/hilbench_analyze.py --run-id my-first-run --passes 1
  python3 scripts/hilbench_analyze.py --run-id my-first-run --out /tmp/reports/
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT = _SCRIPTS_DIR.parent
RUNS_DIR = ROOT / "runs"


def _load_json(path: Path) -> object:
    return json.loads(path.read_text())


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:.3f}"


def _fmt_pct2(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:.0%}"


def _fmt_num(v: object, digits: int = 1) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_int(v: object) -> str:
    if v is None:
        return "—"
    try:
        return str(int(float(v)))
    except (TypeError, ValueError):
        return "—"


def _fmt_ms(v: object) -> str:
    if v is None:
        return "—"
    try:
        seconds = float(v) / 1000.0
    except (TypeError, ValueError):
        return "—"
    if seconds >= 60:
        return f"{seconds / 60.0:.1f} min"
    return f"{seconds:.1f} sec"


def _uid_short(uid: str) -> str:
    return uid[:12] + "…"


def _split_key(key: str) -> tuple[str, str, str]:
    parts = key.split("/", 2)
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    return "—", "—", key


def _row_key(row: dict) -> str:
    return f"{row.get('mode', '—')}/{row.get('agent', '—')}/{row.get('model', '—')}"


def _quality_by_key(pass_rows: list[dict], by_mam: dict) -> dict[str, dict]:
    keys = set(by_mam.keys()) | {_row_key(r) for r in pass_rows}
    quality: dict[str, dict] = {
        k: {
            "attempted_uids": set(),
            "completed_uids": set(),
            "resolved_uids": set(),
            "pass_rows": 0,
            "completed_pass_rows": 0,
            "infra_error_pass_rows": 0,
            "judge_error_pass_rows": 0,
            "resolved_pass_rows": 0,
        }
        for k in keys
    }
    for row in pass_rows:
        key = _row_key(row)
        q = quality.setdefault(
            key,
            {
                "attempted_uids": set(),
                "completed_uids": set(),
                "resolved_uids": set(),
                "pass_rows": 0,
                "completed_pass_rows": 0,
                "infra_error_pass_rows": 0,
                "judge_error_pass_rows": 0,
                "resolved_pass_rows": 0,
            },
        )
        uid = str(row.get("uid", ""))
        status = str(row.get("status", ""))
        if uid:
            q["attempted_uids"].add(uid)
        q["pass_rows"] += 1
        if status == "infra_error":
            q["infra_error_pass_rows"] += 1
        elif status in {"judge_error", "judge_failed"}:
            q["judge_error_pass_rows"] += 1
        else:
            q["completed_pass_rows"] += 1
            if uid:
                q["completed_uids"].add(uid)
        if row.get("resolved"):
            q["resolved_pass_rows"] += 1
            if uid:
                q["resolved_uids"].add(uid)

    serializable: dict[str, dict] = {}
    for key, q in sorted(quality.items()):
        attempted = len(q["attempted_uids"])
        completed = len(q["completed_uids"])
        serializable[key] = {
            "attempted_uids": attempted,
            "completed_uids": completed,
            "resolved_uids": len(q["resolved_uids"]),
            "pass_rows": q["pass_rows"],
            "completed_pass_rows": q["completed_pass_rows"],
            "infra_error_pass_rows": q["infra_error_pass_rows"],
            "judge_error_pass_rows": q["judge_error_pass_rows"],
            "completion_rate": completed / attempted if attempted else None,
        }
    return serializable


def _unique_from_rows(pass_rows: list[dict], field: str) -> list[str]:
    values = {str(r.get(field)) for r in pass_rows if r.get(field) not in (None, "")}
    return sorted(values)


# ── Report builders ───────────────────────────────────────────────────────────

def _build_report(
    run_id: str,
    summary: dict,
    pass_rows: list[dict],
    passes: int,
    out_dir: Path,
) -> str:
    """Return the report.md content as a string."""
    meta = summary.get("metadata", {})
    generated_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Collect per-(mode, agent, model) rows
    by_mam = summary.get("by_mode_agent_model", {})

    modes = _unique_from_rows(pass_rows, "mode") or sorted({_split_key(k)[0] for k in by_mam})
    agents = _unique_from_rows(pass_rows, "agent") or sorted({_split_key(k)[1] for k in by_mam})
    models = _unique_from_rows(pass_rows, "model") or sorted({_split_key(k)[2] for k in by_mam})
    sdk_display = ", ".join(agents) if agents else "—"
    model_display = ", ".join(models) if models else "—"

    # Count unique UIDs
    uid_set = {r["uid"] for r in pass_rows}
    num_uids = len(uid_set) or meta.get("num_uids", "?")

    lines: list[str] = []

    # ── Header ──
    lines += [
        f"# Escalation Lens Report: {run_id}",
        f"",
        f"Generated: {generated_at}  |  SDK: {sdk_display}  |  Model: {model_display}  "
        f"|  Modes: {', '.join(modes) if modes else '—'}  |  UIDs: {num_uids}  |  Passes: {passes}",
        f"",
    ]

    # ── Run quality before scorecard ──
    quality = _quality_by_key(pass_rows, by_mam)
    lines += ["## Run Quality", ""]
    if quality:
        lines.append("| Mode/agent/model | Attempted UIDs | Completed UIDs | Completion | Pass rows | Infra errors | Judge errors |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|")
        for key, q in quality.items():
            lines.append(
                f"| {key} | {q['attempted_uids']} | {q['completed_uids']} | "
                f"{_fmt_pct2(q['completion_rate'])} | {q['pass_rows']} | "
                f"{q['infra_error_pass_rows']} | {q['judge_error_pass_rows']} |"
            )
    else:
        lines.append("No pass-level rows were available; run quality cannot be computed.")
    lines.append("")

    # ── Scorecard table (one column per mode/agent/model) ──
    lines += ["## Scorecard", ""]

    metric_labels = [
        ("pass@1",          "pass_at_1"),
        (f"pass@{passes}",  f"pass_at_{passes}"),
        (f"gated pass@{passes}", f"gated_pass_at_{passes}"),
        ("Ask Precision",   "ask_precision"),
        ("Ask Recall",      "ask_recall"),
        ("Ask F1",          "ask_f1"),
        ("Avg questions/pass", "avg_questions_per_pass"),
        ("Avg steps/pass",  "avg_steps_per_pass"),
    ]

    if len(by_mam) == 1:
        # Single-column scorecard
        key, row = next(iter(by_mam.items()))
        lines.append(f"| Metric | {key} |")
        lines.append("|---|---|")
        for label, field in metric_labels:
            val = row.get(field)
            lines.append(f"| {label} | {_fmt_pct(val)} |")
    else:
        # Multi-column scorecard
        headers = list(by_mam.keys())
        lines.append("| Metric | " + " | ".join(headers) + " |")
        lines.append("|---| " + " | ".join("---" for _ in headers) + " |")
        for label, field in metric_labels:
            vals = [_fmt_pct(by_mam[h].get(field)) for h in headers]
            lines.append(f"| {label} | " + " | ".join(vals) + " |")

    lines.append("")

    # ── Resource usage ──
    lines += ["## Resource Usage", ""]
    if by_mam:
        lines.append("| Mode/agent/model | Wall-clock/pass | LLM calls/pass | Tool calls/pass | Turns/items/pass | Input tokens | Output tokens | Total tokens |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for key, row in by_mam.items():
            lines.append(
                f"| {key} | {_fmt_ms(row.get('avg_wall_clock_ms_per_pass'))} | "
                f"{_fmt_num(row.get('avg_llm_calls_per_pass'))} | "
                f"{_fmt_num(row.get('avg_tool_calls_per_pass'))} | "
                f"{_fmt_num(row.get('avg_turns_or_items_per_pass'))} | "
                f"{_fmt_int(row.get('total_input_tokens'))} | "
                f"{_fmt_int(row.get('total_output_tokens'))} | "
                f"{_fmt_int(row.get('total_tokens'))} |"
            )
    else:
        lines.append("No aggregate metrics were available.")
    lines.append("")

    # ── Ask Behavior ──
    lines += ["## Ask Behavior", ""]
    for key, row in by_mam.items():
        if row.get("ask_f1") is None and row.get("total_questions_full_info") is None:
            continue
        if len(by_mam) > 1:
            lines.append(f"**{key}**")
        if row.get("total_questions_full_info") is not None and row.get("ask_f1") is None:
            lines += [
                f"- Questions asked despite full info: {row.get('total_questions_full_info', 0)}",
                "",
            ]
            continue
        total_q    = row.get("total_questions", 0)
        resolved   = row.get("total_blockers_resolved", 0)
        present    = row.get("total_blockers_present", 0)
        capped     = row.get("total_ask_human_capped", 0)
        cooldown   = row.get("total_ask_human_cooldown_denied", 0)
        lines += [
            f"- Total questions asked: {total_q}",
            f"- Blockers resolved: {resolved} / {present}"
            + (f" ({resolved/present:.0%})" if present else ""),
            f"- Ask-capped (suppressed): {capped}",
            f"- Cooldown-denied: {cooldown}",
            "",
        ]
    if not any((row.get("ask_f1") is not None or row.get("total_questions_full_info") is not None) for row in by_mam.values()):
        lines.append("No ask-behavior metrics were available for these modes.")
        lines.append("")

    # ── Failure Examples ((mode, UID) where resolved=False for all passes) ──
    uid_passes: dict[tuple[str, str], list[bool]] = defaultdict(list)
    for r in pass_rows:
        uid_passes[(r.get("mode", "—"), r["uid"])].append(bool(r.get("resolved", False)))

    failed_uids = [
        key for key, results in sorted(uid_passes.items())
        if not any(results)
    ]
    passing_uids = [
        key for key, results in sorted(uid_passes.items())
        if all(results)
    ]

    lines += [f"## Failure Examples ({len(failed_uids)} mode/UID pairs with 0/{passes} passes resolved)", ""]
    if failed_uids:
        lines.append(f"| Mode | UID | Passes tried | Notes |")
        lines.append("|---|---|---:|---|")
        for mode, uid in failed_uids[:10]:
            n_tried = len(uid_passes[(mode, uid)])
            lines.append(f"| `{mode}` | `{uid}` | {n_tried} | all unresolved |")
        if len(failed_uids) > 10:
            lines.append(f"| *(+{len(failed_uids) - 10} more)* | | | |")
    else:
        lines.append("*(No fully-failed mode/UID pairs)*")
    lines.append("")

    lines += [
        f"## Success Examples ({len(passing_uids)} mode/UID pairs with {passes}/{passes} passes resolved)",
        "",
    ]
    if passing_uids:
        lines.append(", ".join(f"`{mode}/{uid}`" for mode, uid in passing_uids[:5]))
        if len(passing_uids) > 5:
            lines.append(f" *(+{len(passing_uids) - 5} more)*")
    else:
        lines.append("*(None)*")
    lines.append("")

    # ── Data paths ──
    lines += [
        "## Full Data",
        "",
        f"- Pass-level detail: `{out_dir / 'metrics' / 'pass_level.json'}`",
        f"- Aggregated metrics: `{out_dir / 'metrics' / 'summary.json'}`",
        f"- Trajectories: `{out_dir}/<uid>/<mode>/pass_N/trajectory.json`",
        f"- Schema reference: `docs/run_output_schema.md`",
        "",
    ]

    return "\n".join(lines)


def _build_metadata(
    run_id: str,
    summary: dict,
    pass_rows: list[dict],
    passes: int,
    report_path: Path,
    metrics_path: Path,
) -> dict:
    meta = summary.get("metadata", {})
    by_mam = summary.get("by_mode_agent_model", {})

    modes = _unique_from_rows(pass_rows, "mode") or sorted({_split_key(k)[0] for k in by_mam})
    agents = _unique_from_rows(pass_rows, "agent") or sorted({_split_key(k)[1] for k in by_mam})
    models = _unique_from_rows(pass_rows, "model") or sorted({_split_key(k)[2] for k in by_mam})

    uid_set = {r["uid"] for r in pass_rows}
    quality = _quality_by_key(pass_rows, by_mam)
    resources = {
        key: {
            "avg_wall_clock_ms_per_pass": row.get("avg_wall_clock_ms_per_pass"),
            "avg_llm_calls_per_pass": row.get("avg_llm_calls_per_pass"),
            "avg_tool_calls_per_pass": row.get("avg_tool_calls_per_pass"),
            "avg_turns_or_items_per_pass": row.get("avg_turns_or_items_per_pass"),
            "total_input_tokens": row.get("total_input_tokens"),
            "total_output_tokens": row.get("total_output_tokens"),
            "total_tokens": row.get("total_tokens"),
        }
        for key, row in by_mam.items()
    }

    return {
        "run_id":        run_id,
        "generated_at":  datetime.now(tz=timezone.utc).isoformat(),
        "agents":        agents,
        "models":        models,
        "num_uids":      len(uid_set),
        "num_passes":    passes,
        "modes":         modes,
        "run_quality":   quality,
        "resources":     resources,
        "metrics_path":  str(metrics_path),
        "report_path":   str(report_path),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate report.md and metadata.json for a completed HiL-Bench run.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--run-id", required=True, help="Run identifier (subdirectory of runs/).")
    parser.add_argument(
        "--passes", "-k", type=int, default=3,
        help="Number of passes used in the run (for pass@k labels). Default: 3.",
    )
    parser.add_argument(
        "--out", default=None, metavar="DIR",
        help="Output directory for report.md and metadata.json. Default: runs/<run_id>/.",
    )
    args = parser.parse_args()

    run_dir = RUNS_DIR / args.run_id
    if not run_dir.exists():
        print(f"Error: run directory not found: {run_dir}", file=sys.stderr)
        return 1

    metrics_dir = run_dir / "metrics"
    summary_path = metrics_dir / "summary.json"
    pass_level_path = metrics_dir / "pass_level.json"

    if not summary_path.exists():
        print(
            f"Error: {summary_path} not found.\n"
            f"Make sure Phase 3 (metrics) completed for run '{args.run_id}'.\n"
            f"Re-run with: python3 scripts/run_hil_swe.py --run-id {args.run_id} "
            f"--uids ... --modes ... --passes {args.passes} --skip-eval",
            file=sys.stderr,
        )
        return 1

    summary = _load_json(summary_path)
    if pass_level_path.exists():
        pass_rows = _load_json(pass_level_path)
    else:
        print(
            f"Warning: {pass_level_path} not found; report will omit run quality and examples.",
            file=sys.stderr,
        )
        pass_rows = []

    out_dir = Path(args.out) if args.out else run_dir

    report_path   = out_dir / "report.md"
    metadata_path = out_dir / "metadata.json"

    report_md = _build_report(
        run_id=args.run_id,
        summary=summary,
        pass_rows=pass_rows,
        passes=args.passes,
        out_dir=run_dir,
    )
    report_path.write_text(report_md)

    metadata = _build_metadata(
        run_id=args.run_id,
        summary=summary,
        pass_rows=pass_rows,
        passes=args.passes,
        report_path=report_path,
        metrics_path=summary_path,
    )
    metadata_path.write_text(json.dumps(metadata, indent=2))

    print(f"Report written to: {report_path}")
    print(f"Metadata written to: {metadata_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
