#!/usr/bin/env python3
"""Summarize HiL-SWE outcome pass@k from saved runnable evaluator results."""

from __future__ import annotations

import argparse
import ast
import csv
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

try:
    from passk import build_attempts, build_harness_attempts, compute_passk, summarize_rows
    from process_metrics import compute_process_metrics, render_process_summary
except ModuleNotFoundError:
    from .passk import build_attempts, build_harness_attempts, compute_passk, summarize_rows
    from .process_metrics import compute_process_metrics, render_process_summary

ROOT = Path(__file__).resolve().parents[1]
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
STATUS_RE = re.compile(r"\b(PASSED|FAILED|SKIPPED|ERROR|XPASS|XFAIL)\b\s+(test/[^\s]+)")
TEST_NAME_RE = re.compile(r"\b(test/[^\s]+(?:::[^\s]+)+)")
ALL_PASSED_RE = re.compile(r"\b(\d+) passed in [0-9.]+s\b")
PASSING_STATUSES = {"PASSED", "XPASS"}


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def raise_csv_field_limit() -> None:
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 10


def load_jsonish(path: Path) -> Any | None:
    try:
        if path.suffix == ".jsonl":
            return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if path.suffix == ".json":
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return None


def flatten_records(obj: Any) -> list[dict[str, Any]]:
    if isinstance(obj, list):
        return [item for item in obj if isinstance(item, dict)]
    if isinstance(obj, dict):
        records: list[dict[str, Any]] = []
        for key, value in obj.items():
            if isinstance(value, dict):
                merged = dict(value)
                if "instance_id" not in merged:
                    merged["instance_id"] = str(key)
                records.append(merged)
            elif isinstance(value, bool):
                records.append({"prefix": str(key), "resolved": value})
        return records
    return []


def resolved_value(record: dict[str, Any]) -> bool | None:
    for key in ("resolved", "passed", "success", "is_resolved"):
        if key in record:
            return bool(record[key])
    status = str(record.get("status") or record.get("result") or "").lower()
    if status in {"resolved", "pass", "passed", "success", "succeeded"}:
        return True
    if status in {"unresolved", "fail", "failed", "error", "timeout"}:
        return False
    return None


def collect_official_results(run_dir: Path) -> dict[str, bool]:
    results: dict[str, bool] = {}
    hil_results = load_jsonish(run_dir / "official-hil-eval" / "results_by_prefix.json")
    if isinstance(hil_results, dict):
        return {str(key): bool(value) for key, value in hil_results.items() if isinstance(value, bool)}
    official_dir = run_dir / "official-eval"
    for path in sorted(official_dir.rglob("*.json")) + sorted(official_dir.rglob("*.jsonl")):
        parsed = load_jsonish(path)
        if parsed is None:
            continue
        for record in flatten_records(parsed):
            resolved = resolved_value(record)
            if resolved is None:
                continue
            prefix = record.get("prefix") or record.get("prediction_id") or record.get("id")
            instance_id = record.get("instance_id")
            results[str(prefix or instance_id)] = resolved
    return results


def has_official_hil_results(run_dir: Path) -> bool:
    hil_results = load_jsonish(run_dir / "official-hil-eval" / "results_by_prefix.json")
    return isinstance(hil_results, dict) and any(isinstance(value, bool) for value in hil_results.values())


def parse_listish(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, list):
        return {str(item) for item in value}
    text = str(value)
    if not text.strip():
        return set()
    try:
        parsed = ast.literal_eval(text)
        if isinstance(parsed, list):
            return {str(item) for item in parsed}
    except Exception:
        pass
    return {item.strip() for item in text.split(",") if item.strip()}


def strip_ansi(value: str) -> str:
    return ANSI_RE.sub("", value)


def clean_test_name(value: Any) -> str:
    text = strip_ansi(str(value)).strip()
    text = re.sub(r"\[gw\d+\].*$", "", text).strip()
    return text


def base_test_name(value: Any) -> str:
    text = clean_test_name(value)
    return text.split("[", 1)[0]


def test_aliases(value: Any) -> set[str]:
    clean = clean_test_name(value)
    base = base_test_name(clean)
    return {item for item in (clean, base) if item}


def add_status(statuses: dict[str, str], test_name: Any, status: str) -> None:
    for alias in test_aliases(test_name):
        statuses[alias] = status


def statuses_from_output(parsed: Any) -> dict[str, str]:
    statuses: dict[str, str] = {}
    if not isinstance(parsed, dict) or not isinstance(parsed.get("tests"), list):
        return statuses
    for test in parsed["tests"]:
        if isinstance(test, dict) and test.get("name") and test.get("status"):
            add_status(statuses, test["name"], str(test["status"]))
    return statuses


def scheduled_tests_from_log(text: str) -> list[str]:
    marker = "scheduling tests via"
    if marker not in text:
        return []
    block = text.split(marker, 1)[1]
    block = re.split(r"\n\[gw\d+\]", block, 1)[0]
    tests = []
    for line in block.splitlines():
        line = line.strip()
        if line.startswith("test/"):
            tests.append(base_test_name(line))
    return [name for name in dict.fromkeys(tests) if name]


def statuses_from_logs(*paths: Path) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for path in paths:
        if not path.exists():
            continue
        text = strip_ansi(path.read_text(encoding="utf-8", errors="replace"))
        for match in STATUS_RE.finditer(text):
            add_status(statuses, match.group(2), match.group(1))
        scheduled = scheduled_tests_from_log(text)
        if not scheduled:
            scheduled = [name for name in dict.fromkeys(base_test_name(match.group(1)) for match in TEST_NAME_RE.finditer(text)) if name]
        passed_summary = ALL_PASSED_RE.search(text)
        if passed_summary and int(passed_summary.group(1)) == len(scheduled):
            for test_name in scheduled:
                add_status(statuses, test_name, "PASSED")
    return statuses


def required_test_passed(required: str, statuses: dict[str, str]) -> bool:
    for alias in test_aliases(required):
        if statuses.get(alias) in PASSING_STATUSES:
            return True
    return False


def load_raw_samples(samples_path: Path) -> dict[str, dict[str, Any]]:
    if not samples_path.exists():
        return {}
    if samples_path.suffix == ".jsonl":
        rows = load_jsonish(samples_path) or []
        return {str(row["instance_id"]): row for row in rows}
    raise_csv_field_limit()
    with samples_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return {str(row["instance_id"]): row for row in rows}


def collect_attempt_outputs(run_dir: Path, samples_path: Path, predictions_path: Path | None = None) -> dict[str, bool]:
    raw_samples = load_raw_samples(samples_path)
    official_dir = run_dir / "official-eval"
    results: dict[str, bool] = {}
    freshness_floor = predictions_path.stat().st_mtime if predictions_path and predictions_path.exists() else None
    command_path = official_dir / "command.json"
    if command_path.exists():
        command_mtime = command_path.stat().st_mtime
        freshness_floor = command_mtime if freshness_floor is None else max(freshness_floor, command_mtime)
    stale_outputs: list[Path] = []
    for output_path in official_dir.glob("*/*_output.json"):
        if freshness_floor is not None and output_path.stat().st_mtime < freshness_floor:
            stale_outputs.append(output_path)
            continue
        instance_id = output_path.parent.name
        prefix = output_path.name[: -len("_output.json")]
        sample = raw_samples.get(instance_id)
        parsed = load_jsonish(output_path)
        if not sample:
            results[prefix] = False
            continue
        statuses = statuses_from_output(parsed)
        statuses.update(statuses_from_logs(output_path.with_name(f"{prefix}_stdout.log"), output_path.with_name(f"{prefix}_stderr.log")))
        if not statuses:
            results[prefix] = False
            continue
        required = parse_listish(sample.get("fail_to_pass")) | parse_listish(sample.get("pass_to_pass"))
        results[prefix] = all(required_test_passed(test, statuses) for test in required)
    if stale_outputs:
        rendered = "\n".join(f"- {path}" for path in stale_outputs[:10])
        more = "" if len(stale_outputs) <= 10 else f"\n... and {len(stale_outputs) - 10} more"
        raise SystemExit(
            "Stale official evaluator outputs are older than predictions.json or the latest evaluator command. "
            "Rerun evaluation without --reuse-existing or use a fresh RUN_ID.\n"
            f"{rendered}{more}"
        )
    return results


def prediction_key(prediction: dict[str, Any]) -> tuple[str, str, int]:
    return (
        str(prediction.get("harness") or "unknown"),
        str(prediction.get("instance_id") or ""),
        int(prediction.get("attempt_index") or 0),
    )


def predictions_with_failures(run_dir: Path, predictions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = list(predictions)
    seen = {prediction_key(prediction) for prediction in out}
    attempts_index = load_jsonish(run_dir / "attempts-index.json")
    progress = load_jsonish(run_dir / "generation-progress.json")
    failure_sources: list[dict[str, Any]] = []
    for source in (attempts_index, progress):
        if isinstance(source, dict) and isinstance(source.get("failed_jobs"), list):
            failure_sources.extend(item for item in source["failed_jobs"] if isinstance(item, dict))
    run_id = run_dir.name
    if isinstance(attempts_index, dict):
        run_id = str(attempts_index.get("run_id") or run_id)
    for failure in failure_sources:
        harness = str(failure.get("harness") or "unknown")
        instance_id = str(failure.get("instance_id") or "")
        attempt_index = int(failure.get("attempt_index") or 0)
        key = (harness, instance_id, attempt_index)
        if key in seen or not instance_id or attempt_index < 1:
            continue
        out.append(
            {
                "instance_id": instance_id,
                "patch": "",
                "prefix": f"{run_id}__{harness}__{instance_id}__attempt-{attempt_index}",
                "harness": harness,
                "attempt_index": attempt_index,
                "run_id": run_id,
                "sdk_error": failure.get("error"),
                "generation_failed": True,
            }
        )
        seen.add(key)
    return out


def filter_ambiguous_instance_results(predictions: list[dict[str, Any]], official: dict[str, bool]) -> dict[str, bool]:
    counts: dict[str, int] = {}
    for prediction in predictions:
        instance_id = str(prediction.get("instance_id") or "")
        if instance_id:
            counts[instance_id] = counts.get(instance_id, 0) + 1
    ambiguous_instances = {instance_id for instance_id, count in counts.items() if count > 1}
    return {key: value for key, value in official.items() if key not in ambiguous_instances}


def load_hil_evaluator_status(samples_path: Path) -> dict[str, str] | None:
    manifest_path = samples_path.parent / "manifest.json"
    manifest = load_jsonish(manifest_path)
    if not isinstance(manifest, dict):
        return None
    alignment = manifest.get("hil_evaluator_alignment")
    if not isinstance(alignment, dict):
        return None
    by_instance = alignment.get("by_instance")
    if not isinstance(by_instance, dict):
        return None
    statuses: dict[str, str] = {}
    for instance_id, item in by_instance.items():
        if isinstance(item, dict):
            statuses[str(instance_id)] = str(item.get("hil_evaluator_status") or "missing_aligned_tests")
    return statuses


def hil_outcome_results(
    predictions: list[dict[str, Any]],
    swebench_pro_test_results: dict[str, bool],
    hil_evaluator_status_by_instance: dict[str, str] | None,
) -> dict[str, bool]:
    by_instance = build_attempts(predictions, swebench_pro_test_results, hil_evaluator_status_by_instance)
    out: dict[str, bool] = {}
    for attempts in by_instance.values():
        for attempt in attempts:
            if attempt.get("resolved") is not None:
                out[str(attempt.get("prefix") or "")] = bool(attempt["resolved"])
    return {key: value for key, value in out.items() if key}


def attempt_metrics_by_prefix(process_metrics: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("prefix") or ""): item
        for item in process_metrics.get("attempts", [])
        if isinstance(item, dict) and item.get("prefix")
    }


def attempt_dir_for_prediction(run_dir: Path, prediction: dict[str, Any]) -> Path:
    harness = str(prediction.get("harness") or "unknown")
    instance_id = str(prediction.get("instance_id") or "")
    attempt_index = int(prediction.get("attempt_index") or 0)
    direct = run_dir / "trajectories" / harness / instance_id / f"attempt-{attempt_index}"
    if direct.exists():
        return direct
    mode = str(prediction.get("mode") or "")
    if mode:
        with_mode = run_dir / "trajectories" / harness / mode / instance_id / f"attempt-{attempt_index}"
        if with_mode.exists():
            return with_mode
    matches = sorted((run_dir / "trajectories" / harness).glob(f"*/{instance_id}/attempt-{attempt_index}"))
    return matches[0] if matches else direct


def build_author_rows(
    *,
    run_dir: Path,
    predictions: list[dict[str, Any]],
    hil_pass_by_prefix: dict[str, bool],
    process_metrics: dict[str, Any],
    default_mode: str,
) -> list[dict[str, Any]]:
    by_prefix = attempt_metrics_by_prefix(process_metrics)
    rows: list[dict[str, Any]] = []
    for prediction in predictions:
        prefix = str(prediction.get("prefix") or "")
        attempt_metrics = by_prefix.get(prefix, {})
        mode = str(prediction.get("mode") or default_mode or "ask_human")
        model = str(prediction.get("model") or prediction.get("harness") or "unknown")
        status = str(prediction.get("status") or "completed")
        if prediction.get("infra_error"):
            status = "infra_error"
        rows.append(
            {
                "task_type": "swe",
                "task_name": str(prediction.get("instance_id") or ""),
                "model": model,
                "mode": mode,
                "pass_num": int(prediction.get("attempt_index") or attempt_from_prefix_for_summary(prefix) or 0),
                "resolved": bool(hil_pass_by_prefix.get(prefix, False)),
                "status": status,
                "trajectory_dir": str(attempt_dir_for_prediction(run_dir, prediction)),
                "cost": prediction.get("cost") or attempt_metrics.get("cost") or 0.0,
                "num_steps": prediction.get("num_steps") or attempt_metrics.get("event_count") or 0,
                "tokens_sent": prediction.get("tokens_sent") or 0,
                "tokens_received": prediction.get("tokens_received") or 0,
                "num_questions": attempt_metrics.get("clarification_request_count") or 0,
                "num_blockers_resolved": len(set(attempt_metrics.get("matched_blocker_ids") or [])),
                "total_num_blockers": len(set(attempt_metrics.get("registered_blocker_ids") or [])),
            }
        )
    return rows


def attempt_from_prefix_for_summary(prefix: str) -> int | None:
    match = re.search(r"attempt[-_](\d+)", str(prefix))
    return int(match.group(1)) if match else None


def render_metric_lines(metrics: dict[str, Any]) -> list[str]:
    lines: list[str] = [f"Total instances: {metrics['total_instances']}"]
    missing = int(metrics.get("missing_eval_attempts") or 0)
    if missing:
        lines.append(f"Missing eval attempts: {missing}")
    missing_aligned = int(metrics.get("missing_hil_aligned_eval_attempts") or 0)
    if missing_aligned:
        lines.append(f"Missing HiL-aligned evaluator attempts: {missing_aligned}")
    lines.append(f"HiL evaluator coverage: {float(metrics.get('hil_evaluator_coverage') or 0.0):.4f}")
    lines.append("")
    for k, value in metrics["pass_at_k"].items():
        lines.append(f"- HiL-SWE outcome pass@{k}: {value:.4f}")
    for k, value in metrics["unbiased_pass_at_k"].items():
        rendered = "missing" if value is None else f"{value:.4f}"
        lines.append(f"- unbiased HiL-SWE outcome pass@{k}: {rendered}")
    for k, value in (metrics.get("swebench_pro_test_pass_at_k") or {}).items():
        lines.append(f"- diagnostic SWE-Bench Pro test pass@{k}: {value:.4f}")
    for k, value in (metrics.get("unbiased_swebench_pro_test_pass_at_k") or {}).items():
        rendered = "missing" if value is None else f"{value:.4f}"
        lines.append(f"- unbiased diagnostic SWE-Bench Pro test pass@{k}: {rendered}")
    lines.append(f"- diagnostic SWE-Bench Pro test pass attempts: {metrics.get('swebench_pro_test_pass_count', 0)}")
    lines.append(
        f"- underconstrained diagnostic test pass attempts: {metrics.get('ungrounded_or_underconstrained_test_pass_count', 0)}"
    )
    lines.append("")
    lines.append("Per-task attempts:")
    for instance_id, attempts in sorted(metrics["instances"].items()):
        task_success = any(item["resolved"] is True for item in attempts)
        status = ", ".join(
            f"{item['attempt_index']}={'missing' if item['resolved'] is None else item['resolved']}"
            f"/diag={'missing' if item.get('swebench_pro_test_resolved') is None else item.get('swebench_pro_test_resolved')}"
            for item in attempts
        )
        lines.append(f"- {instance_id}: success={task_success}; attempts: {status}")
    return lines


def render_result_line(label: str, metrics: dict[str, Any]) -> str:
    pass_parts = [f"hil_swe_outcome_pass@{k}={value:.4f}" for k, value in metrics["pass_at_k"].items()]
    unbiased_parts = [
        f"unbiased_hil_swe_outcome_pass@{k}={'missing' if value is None else f'{value:.4f}'}"
        for k, value in metrics["unbiased_pass_at_k"].items()
    ]
    diagnostic_parts = [
        f"swebench_pro_test_pass@{k}={value:.4f}" for k, value in (metrics.get("swebench_pro_test_pass_at_k") or {}).items()
    ]
    unbiased_diagnostic_parts = [
        f"unbiased_swebench_pro_test_pass@{k}={'missing' if value is None else f'{value:.4f}'}"
        for k, value in (metrics.get("unbiased_swebench_pro_test_pass_at_k") or {}).items()
    ]
    missing = int(metrics.get("missing_eval_attempts") or 0)
    missing_aligned = int(metrics.get("missing_hil_aligned_eval_attempts") or 0)
    parts = [", ".join(pass_parts), ", ".join(unbiased_parts), ", ".join(diagnostic_parts), ", ".join(unbiased_diagnostic_parts)]
    parts.append(f"hil_evaluator_coverage={float(metrics.get('hil_evaluator_coverage') or 0.0):.4f}")
    parts.append(f"underconstrained_test_pass_attempts={metrics.get('ungrounded_or_underconstrained_test_pass_count', 0)}")
    parts.append(f"missing_eval_attempts={missing}")
    parts.append(f"missing_hil_aligned_eval_attempts={missing_aligned}")
    return f"- {label}: {'; '.join(part for part in parts if part)}"


def render_final_results_lines(metrics: dict[str, Any]) -> list[str]:
    lines = ["## Final Results"]
    if "harnesses" in metrics:
        for harness, harness_metrics in metrics["harnesses"].items():
            lines.append(render_result_line(harness, harness_metrics))
    else:
        lines.append(render_result_line(str(metrics.get("harness") or "overall"), metrics))
    return lines


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument("--samples", type=Path, default=ROOT / "data" / "swebench_pro_samples.csv")
    parser.add_argument("--human-kb", type=Path, default=None)
    parser.add_argument("--k", type=int, action="append", default=None)
    args = parser.parse_args()

    run_dir = args.run_dir or ROOT / "evals" / args.run_id
    predictions_path = run_dir / "predictions.json"
    if not predictions_path.exists():
        raise SystemExit(f"Missing predictions: {predictions_path}")
    predictions = predictions_with_failures(run_dir, json.loads(predictions_path.read_text(encoding="utf-8")))
    swebench_pro_test_results = collect_attempt_outputs(run_dir, args.samples, predictions_path)
    if not swebench_pro_test_results:
        swebench_pro_test_results = filter_ambiguous_instance_results(predictions, collect_official_results(run_dir))
    using_official_hil = has_official_hil_results(run_dir)
    hil_evaluator_status_by_instance = None if using_official_hil else load_hil_evaluator_status(args.samples)
    hil_pass_by_prefix = hil_outcome_results(predictions, swebench_pro_test_results, hil_evaluator_status_by_instance)
    process_metrics = compute_process_metrics(run_dir, args.human_kb, pass_by_prefix=hil_pass_by_prefix)
    progress = load_jsonish(run_dir / "generation-progress.json") or {}
    default_mode = str(progress.get("mode") or ("ask_human" if progress.get("human_kb") else "baseline"))
    expected_passes = max(args.k or [1, 2, 3])
    author_rows = build_author_rows(
        run_dir=run_dir,
        predictions=predictions,
        hil_pass_by_prefix=hil_pass_by_prefix,
        process_metrics=process_metrics,
        default_mode=default_mode,
    )
    author_summary = {
        "metadata": {
            "include_partial": False,
            "num_passes": expected_passes,
            "formula": "hil_bench.run_hil_bench.summarize_rows",
        },
        "SWE": summarize_rows(author_rows, include_partial=False, expected_passes=expected_passes),
    }
    harnesses = sorted({str(prediction.get("harness") or "unknown") for prediction in predictions})
    if len(harnesses) <= 1:
        by_instance = build_attempts(predictions, swebench_pro_test_results, hil_evaluator_status_by_instance)
        metrics = compute_passk(by_instance, args.k or [1, 2, 3])
        metrics["run_id"] = args.run_id
        if harnesses:
            metrics["harness"] = harnesses[0]
    else:
        metrics = {
            "run_id": args.run_id,
            "harnesses": {
                harness: compute_passk(by_instance, args.k or [1, 2, 3])
                for harness, by_instance in build_harness_attempts(
                    predictions, swebench_pro_test_results, hil_evaluator_status_by_instance
                ).items()
            },
        }
    metrics["process_metrics"] = process_metrics
    metrics["author_passk"] = author_summary

    out_json = run_dir / "metrics.json"
    out_md = run_dir / "summary.md"
    atomic_write_text(run_dir / "process_metrics.json", json.dumps(process_metrics, indent=2, sort_keys=True) + "\n")
    atomic_write_text(run_dir / "process_summary.md", render_process_summary(process_metrics))
    atomic_write_text(out_json, json.dumps(metrics, indent=2, sort_keys=True) + "\n")
    lines = [f"# {args.run_id}", ""]
    lines.append("## HiL-Bench Author Pass@k")
    author_swe = author_summary["SWE"]
    if author_swe:
        for mode, models in author_swe.items():
            for model, model_metrics in models.items():
                parts = [
                    f"pass@{k}={model_metrics.get(f'pass_at_{k}', 0.0):.4f} (n={model_metrics.get(f'pass_at_{k}_n', 0)})"
                    for k in range(1, expected_passes + 1)
                ]
                if mode == "ask_human":
                    parts.append(f"ask_f1={model_metrics.get('ask_f1', 0.0):.4f}")
                lines.append(f"- {mode}/{model}: {', '.join(parts)}")
    else:
        lines.append("- no author-compatible rows included")
    lines.append("")
    if "harnesses" in metrics:
        for harness, harness_metrics in metrics["harnesses"].items():
            lines.append(f"## {harness}")
            lines.extend(render_metric_lines(harness_metrics))
            lines.append("")
    else:
        lines.extend(render_metric_lines(metrics))
        lines.append("")
    lines.append("## Process Metrics")
    lines.append(f"- ASK-F1: {process_metrics['ASK_F1']:.4f}")
    lines.append(f"- paper/author ask-F1: {process_metrics.get('paper_ask_f1', 0.0):.4f}")
    lines.append(f"- question precision: {process_metrics['question_precision']:.4f}")
    lines.append(f"- blocker recall: {process_metrics['blocker_recall']:.4f}")
    lines.append(
        f"- Ask-F1 counts: Qrel/Q={process_metrics.get('Qrel_count', process_metrics.get('answered_clarification_count'))}/"
        f"{process_metrics.get('Q_count', process_metrics.get('clarification_request_count'))}; "
        f"Baddr/B={process_metrics.get('Baddr_count', process_metrics.get('addressed_blocker_count'))}/"
        f"{process_metrics.get('B_count', process_metrics.get('registered_blocker_count'))}"
    )
    lines.append(f"- clarification requests/task: {process_metrics['clarification_requests_per_task']:.4f}")
    lines.append(f"- approval/permission requests/task: {process_metrics['approval_permission_requests_per_task']:.4f}")
    human_burden = process_metrics["human_burden_per_successful_task"]
    lines.append(f"- human burden/success: {'missing' if human_burden is None else f'{human_burden:.4f}'}")
    lines.append(f"- approval fallback/registry/unknown: {process_metrics['approval_fallback_count']}/{process_metrics['approval_registry_grounded_count']}/{process_metrics['approval_unknown_count']}")
    lines.append(f"- grounded/ungrounded pass: {process_metrics['grounded_pass_count']}/{process_metrics['ungrounded_pass_count']}")
    lines.append(f"- silent blocker count: {process_metrics['silent_blocker_count']}")
    completeness = ", ".join(f"{key}={value}" for key, value in process_metrics["trace_completeness"].items())
    lines.append(f"- trace completeness: {completeness}")
    if process_metrics["top_deterministic_failure_signals"]:
        signals = "; ".join(f"{item['signal']}={item['count']}" for item in process_metrics["top_deterministic_failure_signals"])
        lines.append(f"- top deterministic failure signals: {signals}")
    else:
        lines.append("- top deterministic failure signals: none")
    lines.append("")
    lines.extend(render_final_results_lines(metrics))
    atomic_write_text(out_md, "\n".join(lines) + "\n")
    print(out_json)
    print(out_md)


if __name__ == "__main__":
    main()
