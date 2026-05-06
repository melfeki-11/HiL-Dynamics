#!/usr/bin/env python3
"""Render a combined HiL-SWE first-N report for full_info and ask_human runs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def metric_at(item: dict[str, Any], key: str, k: int) -> Any:
    value = item.get(key) or {}
    return value.get(str(k))


def fmt(value: Any) -> str:
    if value is None:
        return "missing"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def harness_metrics(run_dir: Path) -> dict[str, dict[str, Any]]:
    metrics = load_json(run_dir / "metrics.json")
    process = load_json(run_dir / "process_metrics.json") if (run_dir / "process_metrics.json").exists() else {}
    harnesses = metrics.get("harnesses") or {}
    process_harnesses = process.get("harnesses") or {}
    out: dict[str, dict[str, Any]] = {}
    for harness, item in sorted(harnesses.items()):
        proc = process_harnesses.get(harness) or {}
        out[harness] = {
            "hil_pass_at_1": metric_at(item, "pass_at_k", 1),
            "hil_pass_at_3": metric_at(item, "pass_at_k", 3),
            "unbiased_hil_pass_at_3": metric_at(item, "unbiased_pass_at_k", 3),
            "diagnostic_pass_at_1": metric_at(item, "swebench_pro_test_pass_at_k", 1),
            "diagnostic_pass_at_3": metric_at(item, "swebench_pro_test_pass_at_k", 3),
            "missing_eval_attempts": item.get("missing_eval_attempts"),
            "missing_hil_aligned_eval_attempts": item.get("missing_hil_aligned_eval_attempts"),
            "hil_evaluator_coverage": item.get("hil_evaluator_coverage"),
            "underconstrained_test_pass_attempts": item.get("ungrounded_or_underconstrained_test_pass_count"),
            "ask_f1": proc.get("ASK_F1"),
            "precision": proc.get("question_precision"),
            "recall": proc.get("blocker_recall"),
            "questions": proc.get("Q_count", proc.get("clarification_request_count")),
            "answered_questions": proc.get("Qrel_count", proc.get("answered_clarification_count")),
            "unknown_questions": proc.get("unknown_clarification_count"),
            "blockers_discovered": proc.get("Baddr_count", proc.get("addressed_blocker_count")),
            "blockers_present": proc.get("B_count", proc.get("registered_blocker_count")),
            "attempt_count": proc.get("attempt_count"),
            "successful_attempt_count": proc.get("successful_attempt_count"),
            "successful_task_count": proc.get("successful_task_count"),
            "top_failure_signals": proc.get("top_deterministic_failure_signals") or [],
        }
    return out


def accepted_model(progress: dict[str, Any], harness: str) -> str:
    if harness == "claude-code":
        return str(progress.get("claude_model") or progress.get("claude-code_model") or "")
    if harness == "codex":
        return str(progress.get("codex_model") or "")
    return ""


def load_audit(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "trajectory_audit.json"
    return load_json(path) if path.exists() else []


def prepared_label(prepared_dir: Path) -> str:
    manifest_path = prepared_dir / "manifest.json"
    if manifest_path.exists():
        manifest = load_json(manifest_path)
        limit = manifest.get("limit")
        if limit:
            return f"First-{limit}"
    name = prepared_dir.name
    marker = "first"
    if marker in name:
        return f"First-{name.rsplit(marker, 1)[-1]}"
    return "Selected"


def question_summary(items: list[dict[str, Any]], harness: str) -> dict[str, Any]:
    selected = [item for item in items if item.get("harness") == harness]
    return {
        "attempts": len(selected),
        "questions": sum(int(item.get("question_count") or 0) for item in selected),
        "matched": sum(int(item.get("relevant_question_count") or 0) for item in selected),
        "empty_patches": sum(1 for item in selected if (item.get("flags") or {}).get("empty_patch")),
        "sdk_errors": sum(1 for item in selected if (item.get("flags") or {}).get("sdk_error")),
        "context_limit_or_timeout": sum(1 for item in selected if (item.get("flags") or {}).get("context_limit_or_timeout")),
        "generated_lock_or_huge": sum(
            1
            for item in selected
            if (item.get("flags") or {}).get("generated_assets")
            or (item.get("flags") or {}).get("lockfile")
            or (item.get("flags") or {}).get("huge_diff")
        ),
    }


def passes_from_progress(progress: dict[str, Any], harness_count: int, num_tasks: Any) -> int | None:
    try:
        total_jobs = int(progress.get("total_jobs") or 0)
        tasks = int(num_tasks or 0)
    except Exception:
        return None
    denominator = harness_count * tasks
    if total_jobs <= 0 or denominator <= 0:
        return None
    return total_jobs // denominator


def render_report(summary: dict[str, Any]) -> str:
    lines = [
        f"# HiL-SWE {summary['selection_label']} Full-Info vs Ask-Human Report",
        "",
        f"- prepared_dir: `{summary['prepared_dir']}`",
        f"- full_info_run_id: `{summary['full_info_run_id']}`",
        f"- ask_human_run_id: `{summary['ask_human_run_id']}`",
        f"- upstream reference: `{summary['upstream_reference']}`",
        f"- selected SWE tasks: {summary['num_tasks']}",
        f"- passes per mode/harness: {summary['passes']}",
        "- Headline HiL outcome pass@k is only fully validated when HiL evaluator coverage is 1.0; diagnostic SWE-Bench Pro test pass@k is reported separately.",
        "",
        "## Outcome Metrics",
        "",
        "| harness | mode | HiL pass@1 | HiL pass@3 | HiL eval coverage | missing aligned eval attempts | diagnostic pass@1 | diagnostic pass@3 | Ask-F1 | precision | recall | Qrel / Q | Baddr / B | successful tasks | SDK/context flags |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for harness in sorted(summary["harnesses"]):
        for mode in ("full_info", "ask_human"):
            item = summary["harnesses"][harness][mode]
            audit = summary["trajectory_audit"][mode].get(harness, {})
            ask_f1 = "n/a" if mode == "full_info" else fmt(item.get("ask_f1"))
            precision = "n/a" if mode == "full_info" else fmt(item.get("precision"))
            recall = "n/a" if mode == "full_info" else fmt(item.get("recall"))
            lines.append(
                "| "
                + " | ".join(
                    [
                        harness,
                        mode,
                        fmt(item.get("hil_pass_at_1")),
                        fmt(item.get("hil_pass_at_3")),
                        fmt(item.get("hil_evaluator_coverage")),
                        fmt(item.get("missing_hil_aligned_eval_attempts")),
                        fmt(item.get("diagnostic_pass_at_1")),
                        fmt(item.get("diagnostic_pass_at_3")),
                        ask_f1,
                        precision,
                        recall,
                        f"{fmt(item.get('answered_questions'))} / {fmt(item.get('questions'))}",
                        f"{fmt(item.get('blockers_discovered'))} / {fmt(item.get('blockers_present'))}",
                        fmt(item.get("successful_task_count")),
                        f"sdk={audit.get('sdk_errors', 0)}, ctx={audit.get('context_limit_or_timeout', 0)}, patch={audit.get('generated_lock_or_huge', 0)}",
                    ]
                )
                + " |"
            )
    lines.extend(["", "## Model Configuration", ""])
    for harness, models in sorted(summary["models"].items()):
        lines.append(f"- {harness}: {models}")
    lines.extend(["", "## Trajectory Checks", ""])
    for mode in ("full_info", "ask_human"):
        lines.append(f"### {mode}")
        for harness, audit in sorted(summary["trajectory_audit"][mode].items()):
            lines.append(
                f"- {harness}: attempts={audit['attempts']}, questions={audit['questions']}, matched={audit['matched']}, "
                f"empty_patches={audit['empty_patches']}, sdk_errors={audit['sdk_errors']}, "
                f"context_limit_or_timeout={audit['context_limit_or_timeout']}, generated_lock_or_huge={audit['generated_lock_or_huge']}"
            )
    lines.extend(["", "## Artifacts", ""])
    for label, path in summary["artifacts"].items():
        lines.append(f"- {label}: `{path}`")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full-info-run-id", required=True)
    parser.add_argument("--ask-human-run-id", required=True)
    parser.add_argument("--prepared-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, default=None)
    args = parser.parse_args()

    full_info_dir = ROOT / "evals" / args.full_info_run_id
    ask_human_dir = ROOT / "evals" / args.ask_human_run_id
    full_info_progress = load_json(full_info_dir / "generation-progress.json")
    ask_human_progress = load_json(ask_human_dir / "generation-progress.json")
    prepared_manifest = load_json(args.prepared_dir / "manifest.json") if (args.prepared_dir / "manifest.json").exists() else {}
    full_info = harness_metrics(full_info_dir)
    ask_human = harness_metrics(ask_human_dir)
    harnesses = sorted(set(full_info) | set(ask_human))
    full_audit = load_audit(full_info_dir)
    ask_audit = load_audit(ask_human_dir)
    out_dir = args.out_dir or (ROOT / "evals" / f"{args.ask_human_run_id}__combined")
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "prepared_dir": rel(args.prepared_dir),
        "selection_label": prepared_label(args.prepared_dir),
        "num_tasks": prepared_manifest.get("limit"),
        "passes": passes_from_progress(ask_human_progress, len(harnesses), prepared_manifest.get("limit")),
        "full_info_run_id": args.full_info_run_id,
        "ask_human_run_id": args.ask_human_run_id,
        "upstream_reference": "https://github.com/hilbenchauthors/hil-bench/tree/master",
        "harnesses": {
            harness: {
                "full_info": full_info.get(harness, {}),
                "ask_human": ask_human.get(harness, {}),
            }
            for harness in harnesses
        },
        "models": {
            "claude-code": {
                "full_info": accepted_model(full_info_progress, "claude-code"),
                "ask_human": accepted_model(ask_human_progress, "claude-code"),
                "thinking": ask_human_progress.get("claude_thinking") or full_info_progress.get("claude_thinking"),
                "effort": ask_human_progress.get("claude_effort") or full_info_progress.get("claude_effort"),
            },
            "codex": {
                "full_info": accepted_model(full_info_progress, "codex"),
                "ask_human": accepted_model(ask_human_progress, "codex"),
                "reasoning_effort": ask_human_progress.get("model_reasoning_effort") or full_info_progress.get("model_reasoning_effort"),
            },
        },
        "trajectory_audit": {
            "full_info": {harness: question_summary(full_audit, harness) for harness in harnesses},
            "ask_human": {harness: question_summary(ask_audit, harness) for harness in harnesses},
        },
        "artifacts": {
            "full_info_metrics": rel(full_info_dir / "metrics.json"),
            "ask_human_metrics": rel(ask_human_dir / "metrics.json"),
            "full_info_audit": rel(full_info_dir / "trajectory_audit.json"),
            "ask_human_audit": rel(ask_human_dir / "trajectory_audit.json"),
            "combined_report_json": rel(out_dir / "final_report.json"),
            "combined_report_md": rel(out_dir / "final_report.md"),
        },
    }
    (out_dir / "final_report.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out_dir / "final_report.md").write_text(render_report(summary), encoding="utf-8")
    print(out_dir / "final_report.md")
    print(out_dir / "final_report.json")


if __name__ == "__main__":
    main()
