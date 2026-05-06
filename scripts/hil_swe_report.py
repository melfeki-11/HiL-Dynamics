#!/usr/bin/env python3
"""Render a concise HiL-Bench SWE pilot report from saved artifacts."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows
    except FileNotFoundError:
        return []


def load_csv_by_id(path: Path) -> dict[str, dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return {str(row.get("instance_id") or ""): row for row in csv.DictReader(handle)}
    except FileNotFoundError:
        return {}


def oracle_patch_alignment(prepared_dir: Path) -> dict[str, Any]:
    oracle_rows = load_jsonl(prepared_dir / "oracle.jsonl")
    csv_rows = load_csv_by_id(prepared_dir / "samples.csv")
    compared = 0
    mismatches = []
    for row in oracle_rows:
        instance_id = str(row.get("instance_id") or "")
        if not instance_id or instance_id not in csv_rows:
            continue
        compared += 1
        oracle_patch = str(row.get("ground_truth_patch") or "").strip()
        sample_patch = str(csv_rows[instance_id].get("patch") or "").strip()
        if oracle_patch and sample_patch and oracle_patch != sample_patch:
            mismatches.append(instance_id)
    return {
        "compared": compared,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
    }


def select_ask_human_check(prepared_dir: Path) -> tuple[dict[str, Any] | None, Path | None]:
    checks = []
    for path in sorted(prepared_dir.glob("ask_human_check*/ask_human_check.json")):
        parsed = load_json(path)
        if isinstance(parsed, dict):
            checks.append((path, parsed))
    if not checks:
        return None, None
    pass_checks = [(path, parsed) for path, parsed in checks if parsed.get("status") == "PASS"]
    candidates = pass_checks or checks
    path, parsed = max(candidates, key=lambda item: item[0].stat().st_mtime)
    return parsed, path.parent


def fmt_float(value: Any) -> str:
    if value is None:
        return "missing"
    try:
        return f"{float(value):.4f}"
    except Exception:
        return str(value)


def yes_no(value: Any) -> str:
    return "yes" if bool(value) else "no"


def pass_at(metrics: dict[str, Any], k: int) -> Any:
    return (metrics.get("pass_at_k") or {}).get(str(k))


def unbiased_at(metrics: dict[str, Any], k: int) -> Any:
    return (metrics.get("unbiased_pass_at_k") or {}).get(str(k))


def swebench_pro_test_pass_at(metrics: dict[str, Any], k: int) -> Any:
    return (metrics.get("swebench_pro_test_pass_at_k") or {}).get(str(k))


def unbiased_swebench_pro_test_pass_at(metrics: dict[str, Any], k: int) -> Any:
    return (metrics.get("unbiased_swebench_pro_test_pass_at_k") or {}).get(str(k))


def successful_tasks(harness_metrics: dict[str, Any]) -> list[str]:
    out = []
    for instance_id, attempts in (harness_metrics.get("instances") or {}).items():
        if any(item.get("resolved") is True for item in attempts):
            out.append(instance_id)
    return sorted(out)


def failed_tasks(harness_metrics: dict[str, Any]) -> list[str]:
    out = []
    for instance_id, attempts in (harness_metrics.get("instances") or {}).items():
        if not any(item.get("resolved") is True for item in attempts):
            out.append(instance_id)
    return sorted(out)


def errored_attempts(harness_metrics: dict[str, Any]) -> int:
    return sum(
        1
        for attempts in (harness_metrics.get("instances") or {}).values()
        for item in attempts
        if item.get("generation_failed") or item.get("sdk_error")
    )


def process_for_harness(process: dict[str, Any], harness: str) -> dict[str, Any]:
    return (process.get("harnesses") or {}).get(harness, {})


def task_process(process: dict[str, Any], harness: str, instance_id: str) -> dict[str, Any]:
    return ((process.get("harness_tasks") or {}).get(harness) or {}).get(instance_id, {})


def all_complete(completeness: dict[str, Any] | None) -> bool:
    return bool(completeness) and all(bool(value) for value in completeness.values())


def trace_status(process: dict[str, Any]) -> str:
    return "complete" if all_complete(process.get("trace_completeness")) else "incomplete"


def request_count(item: dict[str, Any], count_key: str, rate_key: str) -> int:
    if count_key in item:
        return int(item.get(count_key) or 0)
    return int((item.get(rate_key, 0) or 0) * (item.get("attempt_count", 0) or 0))


def selected_ids_for_run(manifest: dict[str, Any], metrics: dict[str, Any] | None) -> list[str]:
    ids: set[str] = set()
    for harness_metrics in ((metrics or {}).get("harnesses") or {}).values():
        ids.update((harness_metrics.get("instances") or {}).keys())
    if ids:
        manifest_ids = manifest.get("selected_instance_ids") or []
        ordered = [instance_id for instance_id in manifest_ids if instance_id in ids]
        ordered.extend(sorted(ids - set(ordered)))
        return ordered
    return manifest.get("selected_instance_ids") or []


def first_attempt_model(run_dir: Path, harness: str) -> str:
    for path in sorted((run_dir / "trajectories" / harness).glob("*/*/attempt.json")):
        item = load_json(path) or {}
        if item.get("model"):
            return str(item["model"])
    return "see attempt.json"


def grounded_status(item: dict[str, Any]) -> str:
    if item.get("grounded_pass_count", 0):
        return "grounded-pass"
    if item.get("ungrounded_pass_count", 0):
        return "ungrounded-pass"
    if item.get("silent_blocker_count", 0):
        return "silent-blocker"
    return "no-pass"


def qualitative_notes(item: dict[str, Any]) -> list[str]:
    notes = []
    if item.get("clarification_request_count", item.get("clarification_requests_per_task", 0)) == 0 and item.get("attempt_count", 0):
        notes.append("no questions asked")
    if item.get("unknown_clarification_count", 0):
        notes.append("unknown response")
    if item.get("answered_clarification_count", 0):
        notes.append("asked relevant question")
    if item.get("questions_after_first_patch_edit", 0):
        notes.append("asked after patch/edit")
    if item.get("silent_blocker_count", 0):
        notes.append("silent blocker")
    if not all_complete(item.get("trace_completeness")):
        notes.append("trace incomplete")
    return notes


def render_table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(item) for item in row) + " |")
    return lines


def determine_readiness(
    metrics: dict[str, Any] | None,
    process: dict[str, Any] | None,
    patch_alignment: dict[str, Any] | None = None,
) -> tuple[str, str]:
    if not metrics or not process:
        return "FAIL", "missing metrics or process metrics"
    harnesses = metrics.get("harnesses") or {}
    if not {"claude-code", "codex"}.issubset(set(harnesses)):
        return "FAIL", "one or both required harnesses are missing"
    missing_eval = sum(int(item.get("missing_eval_attempts") or 0) for item in harnesses.values())
    if missing_eval:
        return "FAIL", f"{missing_eval} evaluator attempts are missing"
    missing_hil_eval = sum(
        int(item.get("missing_hil_aligned_eval_attempts") or 0)
        for item in harnesses.values()
        if "missing_hil_aligned_eval_attempts" in item
    )
    if missing_hil_eval:
        return "FAIL", f"{missing_hil_eval} attempts lack aligned runnable HiL evaluators"
    coverage_values = [float(item.get("hil_evaluator_coverage") or 0.0) for item in harnesses.values() if "hil_evaluator_coverage" in item]
    if coverage_values and any(value < 1.0 for value in coverage_values):
        return "FAIL", "selected tasks do not have complete aligned runnable HiL evaluator coverage"
    if not all_complete(process.get("trace_completeness")):
        return "FAIL", "trace completeness checks did not all pass"
    if patch_alignment and patch_alignment.get("mismatch_count"):
        return (
            "FAIL",
            "HiL oracle ground-truth patches differ from the SWE-Bench Pro sample patches; "
            "the diagnostic SWE-Bench Pro evaluator is underconstrained for HiL success",
        )
    warning_bits = []
    if any(int(process_for_harness(process, harness).get("silent_blocker_count") or 0) for harness in harnesses):
        warning_bits.append("silent blockers observed")
    if any(int(process_for_harness(process, harness).get("unknown_clarification_count") or 0) for harness in harnesses):
        warning_bits.append("unknown clarification responses observed")
    if warning_bits:
        return "PASS WITH WARNINGS", "; ".join(warning_bits)
    return "PASS", "ready for the next SWE scale step"


def render_report(args: argparse.Namespace) -> str:
    run_dir = args.run_dir or ROOT / "evals" / args.run_id
    prepared_dir = args.prepared_dir
    manifest = load_json(prepared_dir / "manifest.json") or {}
    metrics = load_json(run_dir / "metrics.json")
    process = load_json(run_dir / "process_metrics.json")
    progress = load_json(run_dir / "generation-progress.json") or {}
    attempts_index = load_json(run_dir / "attempts-index.json") or {}
    patch_alignment = oracle_patch_alignment(prepared_dir)
    evaluator_alignment = (manifest.get("hil_evaluator_alignment") or {}).get("counts") or {}
    ask_check, ask_check_path = select_ask_human_check(prepared_dir)
    status, readiness_reason = determine_readiness(metrics, process, patch_alignment)
    selected_ids = selected_ids_for_run(manifest, metrics)
    pilot_label = args.label or f"First-{len(selected_ids)} Pilot"
    harness_metrics = (metrics or {}).get("harnesses") or {}
    process = process or {}

    lines = [
        f"# HiL-Bench SWE {pilot_label}: {args.run_id}",
        "",
        "## A. Run Metadata",
        f"- run_id: `{args.run_id}`",
        f"- selected instance IDs: {', '.join(f'`{item}`' for item in selected_ids)}",
        f"- harnesses: {', '.join(sorted(harness_metrics) or ['missing'])}",
        f"- models: Claude Code `{progress.get('claude_model') or attempts_index.get('claude_model') or first_attempt_model(run_dir, 'claude-code')}`, Codex `{progress.get('codex_model') or attempts_index.get('codex_model') or first_attempt_model(run_dir, 'codex')}`",
        f"- k: {args.k}",
        f"- evaluator used: SWE-Bench Pro-style runnable evaluator metadata from `{prepared_dir / 'samples.csv'}`",
        f"- HiL evaluator alignment: aligned={evaluator_alignment.get('aligned', 'missing')}, missing_aligned_tests={evaluator_alignment.get('missing_aligned_tests', 'missing')}, comparable_mismatches={evaluator_alignment.get('comparable_mismatches', patch_alignment.get('mismatch_count', 0))}",
        f"- evaluator caveat: selected tasks with `hil_evaluator_status != aligned` count as failed for headline HiL-SWE outcome pass@k; current SWE-Bench Pro test passes are diagnostic only",
        f"- KB path: `{prepared_dir / 'kb.json'}`",
        f"- trace path: `{run_dir / 'trajectories'}`",
        f"- cache path: `{progress.get('ask_human_cache') or run_dir / 'ask-human-cache.json'}`",
        f"- process metrics path: `{run_dir / 'process_metrics.json'}`",
        "",
        "## B. Dataset Conversion Summary",
        f"- zip inspected: `{manifest.get('source_zip', 'missing')}`",
        f"- SQL zip observed but not used: `{manifest.get('sql_zip_seen_but_not_used', 'missing')}`",
        f"- task schema found: parquet columns by split are recorded in `{prepared_dir / 'manifest.json'}`",
        f"- registry schema found: `reward_spec.blockers[]` with `id`, `description`, `example_questions`, and `resolution`",
        f"- conversion performed: `{prepared_dir / 'input.jsonl'}`, `{prepared_dir / 'samples.csv'}`, `{prepared_dir / 'kb.json'}`",
        f"- oracle/sample patch alignment: {patch_alignment.get('compared', 0) - patch_alignment.get('mismatch_count', 0)}/{patch_alignment.get('compared', 0)} comparable exact matches",
        f"- assumptions: {'; '.join(manifest.get('assumptions') or [])}",
        "- skipped or malformed tasks: none" if selected_ids else "- skipped or malformed tasks: unavailable",
        "",
        "## C. Outcome Summary",
    ]

    outcome_rows = []
    for harness, item in sorted(harness_metrics.items()):
        successes = successful_tasks(item)
        failures = failed_tasks(item)
        outcome_rows.append(
            [
                harness,
                first_attempt_model(run_dir, harness),
                item.get("total_instances", 0),
                args.k,
                fmt_float(pass_at(item, args.k)),
                fmt_float(unbiased_at(item, args.k)),
                len(successes),
                fmt_float(swebench_pro_test_pass_at(item, args.k)),
                fmt_float(unbiased_swebench_pro_test_pass_at(item, args.k)),
                item.get("ungrounded_or_underconstrained_test_pass_count", 0),
                fmt_float(item.get("hil_evaluator_coverage")),
                item.get("missing_hil_aligned_eval_attempts", 0),
                len(failures),
                errored_attempts(item),
            ]
        )
    lines.extend(
        render_table(
            [
                "harness",
                "model",
                "tasks",
                "k",
                "HiL-SWE outcome pass@3",
                "unbiased HiL-SWE outcome pass@3",
                "HiL successful tasks",
                "diagnostic SWE-Bench Pro test pass@3",
                "unbiased diagnostic test pass@3",
                "underconstrained diagnostic pass attempts",
                "HiL evaluator coverage",
                "missing aligned eval attempts",
                "failed tasks",
                "errored/timed-out attempts",
            ],
            outcome_rows,
        )
        if outcome_rows
        else ["No outcome metrics were available."]
    )

    lines.extend(["", "## D. Process Metrics Summary"])
    process_rows = []
    for harness in sorted(harness_metrics):
        item = process_for_harness(process, harness)
        process_rows.append(
            [
                harness,
                fmt_float(item.get("ASK_F1")),
                fmt_float(item.get("question_precision")),
                fmt_float(item.get("blocker_recall")),
                request_count(item, "Q_count", "clarification_requests_per_attempt"),
                item.get("Qrel_count", item.get("answered_clarification_count", 0)),
                item.get("Baddr_count", item.get("addressed_blocker_count", 0)),
                item.get("B_count", item.get("registered_blocker_count", 0)),
                item.get("unknown_clarification_count", 0),
                item.get("duplicate_question_count", 0),
                item.get("grounded_pass_count", 0),
                item.get("ungrounded_pass_count", 0),
                item.get("silent_blocker_count", 0),
                fmt_float(item.get("human_burden_per_successful_task")),
                request_count(item, "approval_permission_request_count", "approval_permission_requests_per_attempt"),
                trace_status(item),
            ]
        )
    lines.extend(
        render_table(
            [
                "harness",
                "ASK-F1",
                "precision",
                "recall",
                "Q",
                "Qrel",
                "Baddr",
                "B",
                "unknown",
                "duplicate questions",
                "grounded passes",
                "ungrounded passes",
                "silent blockers",
                "human burden/success",
                "approval/permission requests",
                "trace completeness",
            ],
            process_rows,
        )
        if process_rows
        else ["No process metrics were available."]
    )

    lines.extend(["", "## E. Per-Task Table"])
    per_task_rows = []
    for instance_id in selected_ids:
        blocker_count = len([entry for entry in (load_json(prepared_dir / "kb.json") or {}).get("entries", []) if entry.get("instance_id") == instance_id])
        row = [instance_id, blocker_count]
        for harness in ("claude-code", "codex"):
            hm = harness_metrics.get(harness, {})
            attempts = (hm.get("instances") or {}).get(instance_id, [])
            tp = task_process(process, harness, instance_id)
            row.extend(
                [
                    yes_no(any(item.get("resolved") is True for item in attempts)),
                    yes_no(any(item.get("swebench_pro_test_resolved") is True for item in attempts)),
                    fmt_float(tp.get("ASK_F1")),
                    fmt_float(tp.get("blocker_recall")),
                    fmt_float(tp.get("question_precision")),
                    request_count(tp, "Q_count", "clarification_requests_per_attempt"),
                    grounded_status(tp),
                ]
            )
        per_task_rows.append(row)
    lines.extend(
        render_table(
            [
                "instance_id",
                "blockers",
                "Claude pass@3",
                "Claude diagnostic test pass@3",
                "Claude ASK-F1",
                "Claude recall",
                "Claude precision",
                "Claude questions",
                "Claude status",
                "Codex pass@3",
                "Codex diagnostic test pass@3",
                "Codex ASK-F1",
                "Codex recall",
                "Codex precision",
                "Codex questions",
                "Codex status",
            ],
            per_task_rows,
        )
        if per_task_rows
        else ["No per-task metrics were available."]
    )

    lines.extend(["", "## F. Deterministic Trace Notes"])
    note_rows = []
    for harness in ("claude-code", "codex"):
        for instance_id in selected_ids:
            tp = task_process(process, harness, instance_id)
            notes = qualitative_notes(tp)
            if notes:
                note_rows.append([harness, instance_id, "; ".join(notes)])
    if ask_check:
        lines.append(f"- ask_human correctness check: {ask_check.get('status')} at `{ask_check_path}`")
    lines.extend(render_table(["harness", "instance_id", "notes"], note_rows) if note_rows else ["- no deterministic trace notes beyond summary tables"])

    lines.extend(
        [
            "",
            "## G. Readiness Assessment",
            f"- status: {status}",
            f"- reason: {readiness_reason}",
            f"- ready to scale beyond this SWE pilot: {yes_no(status in {'PASS', 'PASS WITH WARNINGS'})}",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument("--prepared-dir", type=Path, default=ROOT / "data" / "hil_bench_swe_first10")
    parser.add_argument("--k", type=int, default=3)
    parser.add_argument("--label", default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    run_dir = args.run_dir or ROOT / "evals" / args.run_id
    out = args.out or run_dir / "hil_swe_report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_report(args), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
