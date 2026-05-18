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


def _uid_short(uid: str) -> str:
    return uid[:12] + "…"


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

    # Infer SDK / model from first key for header
    sdk_display = "—"
    model_display = "—"
    if by_mam:
        first_key = next(iter(by_mam))
        parts = first_key.split("/")
        if len(parts) >= 3:
            sdk_display = parts[1]
            model_display = parts[2]

    # Count unique UIDs
    uid_set = {r["uid"] for r in pass_rows}
    num_uids = len(uid_set) or meta.get("num_uids", "?")

    lines: list[str] = []

    # ── Header ──
    lines += [
        f"# HiL-Bench Report: {run_id}",
        f"",
        f"Generated: {generated_at}  |  SDK: {sdk_display}  |  Model: {model_display}  "
        f"|  UIDs: {num_uids}  |  Passes: {passes}",
        f"",
    ]

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

    # ── Ask Behavior ──
    lines += ["## Ask Behavior", ""]
    for key, row in by_mam.items():
        if len(by_mam) > 1:
            lines.append(f"**{key}**")
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

    # ── Failure Examples (UIDs where resolved=False for all passes) ──
    uid_passes: dict[str, list[bool]] = defaultdict(list)
    for r in pass_rows:
        uid_passes[r["uid"]].append(bool(r.get("resolved", False)))

    failed_uids = [
        uid for uid, results in sorted(uid_passes.items())
        if not any(results)
    ]
    passing_uids = [
        uid for uid, results in sorted(uid_passes.items())
        if all(results)
    ]

    lines += [f"## Failure Examples ({len(failed_uids)} UIDs with 0/{passes} passes resolved)", ""]
    if failed_uids:
        lines.append(f"| UID | Passes tried | Notes |")
        lines.append("|---|---|---|")
        for uid in failed_uids[:10]:
            n_tried = len(uid_passes[uid])
            lines.append(f"| `{uid}` | {n_tried} | all unresolved |")
        if len(failed_uids) > 10:
            lines.append(f"| *(+{len(failed_uids) - 10} more)* | | |")
    else:
        lines.append("*(No fully-failed UIDs)*")
    lines.append("")

    lines += [
        f"## Success Examples ({len(passing_uids)} UIDs with {passes}/{passes} passes resolved)",
        "",
    ]
    if passing_uids:
        lines.append(", ".join(f"`{uid}`" for uid in passing_uids[:5]))
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

    sdk = "—"
    model = "—"
    modes: list[str] = []
    if by_mam:
        first_key = next(iter(by_mam))
        parts = first_key.split("/")
        if len(parts) >= 3:
            modes = [parts[0]]
            sdk   = parts[1]
            model = parts[2]

    uid_set = {r["uid"] for r in pass_rows}

    return {
        "run_id":        run_id,
        "generated_at":  datetime.now(tz=timezone.utc).isoformat(),
        "sdk":           sdk,
        "model":         model,
        "num_uids":      len(uid_set),
        "num_passes":    passes,
        "modes":         modes or list({r.get("mode", "ask_human") for r in pass_rows}),
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
    pass_rows = _load_json(pass_level_path) if pass_level_path.exists() else []

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
