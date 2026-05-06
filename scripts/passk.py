"""pass@k aggregation helpers."""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

TRAJECTORY_RERUN_OCCURRENCE_THRESHOLD_LENIENT = 3
TRAJECTORY_RERUN_OCCURRENCE_THRESHOLD_STRICT = 1
TRAJECTORY_TIMEOUT_OBS_RE = re.compile(r"Command '\[.*\]' timed out after \d+ seconds")
TRAJECTORY_HICCUP_OBS = "can't answer (perhaps transient hiccup)"
TRAJECTORY_ENV_DIED_OBS = "Environment died unexpectedly"
TRAJECTORY_UNKNOWN_ERROR = "Exit due to unknown error"
KB_QUERY_ERROR = "Error querying knowledge base"
SQL_QUOTING_BUG_MARKERS = (
    ("get_database_info", "Error: database $"),
    ("get_table_info", "Error: table $"),
    ("get_column_info", "Error: column $"),
    ("get_business_info", "No business information found matching '$"),
)


def attempt_from_prefix(prefix: str) -> int | None:
    import re

    match = re.search(r"attempt[-_](\d+)", prefix)
    return int(match.group(1)) if match else None


def unbiased_estimate(n: int, c: int, k: int) -> float:
    if n < k:
        raise ValueError("n must be >= k")
    if c == 0:
        return 0.0
    if n - c < k:
        return 1.0
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


def build_attempts(
    predictions: list[dict[str, Any]],
    swebench_pro_test_results: dict[str, bool],
    hil_evaluator_status_by_instance: dict[str, str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    by_instance: dict[str, list[dict[str, Any]]] = defaultdict(list)
    prediction_count_by_instance: dict[str, int] = defaultdict(int)
    for prediction in predictions:
        prediction_count_by_instance[str(prediction["instance_id"])] += 1
    for idx, prediction in enumerate(predictions):
        prefix = str(prediction.get("prefix") or "")
        instance_id = str(prediction["instance_id"])
        attempt_index = prediction.get("attempt_index")
        if attempt_index is None:
            attempt_index = attempt_from_prefix(prefix) or idx + 1
        swebench_pro_test_resolved = swebench_pro_test_results.get(prefix)
        if swebench_pro_test_resolved is None and prediction_count_by_instance[instance_id] == 1:
            swebench_pro_test_resolved = swebench_pro_test_results.get(instance_id)
        hil_evaluator_status = "aligned"
        if hil_evaluator_status_by_instance is not None:
            hil_evaluator_status = hil_evaluator_status_by_instance.get(instance_id, "missing_aligned_tests")
        hil_evaluator_aligned = hil_evaluator_status == "aligned"
        resolved = None
        if swebench_pro_test_resolved is not None:
            resolved = bool(swebench_pro_test_resolved and hil_evaluator_aligned)
        by_instance[instance_id].append(
            {
                "prefix": prefix,
                "attempt_index": int(attempt_index),
                "resolved": resolved,
                "swebench_pro_test_resolved": swebench_pro_test_resolved,
                "eval_missing": swebench_pro_test_resolved is None,
                "hil_evaluator_status": hil_evaluator_status,
                "hil_evaluator_aligned": hil_evaluator_aligned,
                "harness": prediction.get("harness"),
                "generation_failed": bool(prediction.get("generation_failed")),
                "sdk_error": prediction.get("sdk_error"),
            }
        )
    for attempts in by_instance.values():
        attempts.sort(key=lambda item: (item["attempt_index"], str(item.get("prefix") or "")))
    return by_instance


def build_harness_attempts(
    predictions: list[dict[str, Any]],
    swebench_pro_test_results: dict[str, bool],
    hil_evaluator_status_by_instance: dict[str, str] | None = None,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    by_harness: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for prediction in predictions:
        by_harness[str(prediction.get("harness") or "unknown")].append(prediction)
    return {
        harness: build_attempts(items, swebench_pro_test_results, hil_evaluator_status_by_instance)
        for harness, items in sorted(by_harness.items())
    }


def safe_mean(values: list[float | int | None]) -> float | None:
    valid = [float(value) for value in values if value is not None]
    return sum(valid) / len(valid) if valid else None


def stringify_trajectory_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=True)
    except Exception:
        return str(value)


def load_normalized_trajectory_steps_from_dir(trajectory_dir: str | None) -> list[dict[str, str]]:
    if not trajectory_dir:
        return []
    traj_dir = Path(trajectory_dir)
    trajectory_file = traj_dir / "trajectory.jsonl"
    if not trajectory_file.exists():
        return []
    steps: list[dict[str, str]] = []
    try:
        for line in trajectory_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            act = event.get("tool_name") or event.get("content") or event.get("type") or ""
            obs = event.get("observation") or event.get("answer") or event.get("content") or ""
            step = {
                "act": stringify_trajectory_value(act),
                "obs": stringify_trajectory_value(obs),
            }
            for key in ("response", "native_payload", "tool_args", "event_type", "final_status"):
                if event.get(key) not in (None, ""):
                    step[key] = stringify_trajectory_value(event.get(key))
            steps.append(step)
    except Exception:
        return []
    return steps


def trajectory_has_timeout_obs(trajectory: list[dict[str, str]]) -> bool:
    count = 0
    for step in trajectory:
        obs = step.get("obs", "")
        if isinstance(obs, str) and TRAJECTORY_TIMEOUT_OBS_RE.search(obs):
            count += 1
    return count >= TRAJECTORY_RERUN_OCCURRENCE_THRESHOLD_LENIENT


def trajectory_has_hiccup_obs(trajectory: list[dict[str, str]]) -> bool:
    count = 0
    for step in trajectory:
        obs = step.get("obs", "")
        if isinstance(obs, str) and obs.strip() == TRAJECTORY_HICCUP_OBS:
            count += 1
    return count >= TRAJECTORY_RERUN_OCCURRENCE_THRESHOLD_STRICT


def trajectory_has_env_died_obs(trajectory: list[dict[str, str]]) -> bool:
    if not trajectory:
        return False
    obs = trajectory[-1].get("obs", "")
    return isinstance(obs, str) and TRAJECTORY_ENV_DIED_OBS in obs


def trajectory_has_unknown_error(trajectory: Any) -> bool:
    if not isinstance(trajectory, list) or not trajectory:
        return False
    last_step = trajectory[-1]
    if not isinstance(last_step, dict):
        return False
    response = last_step.get("response", "")
    return isinstance(response, str) and TRAJECTORY_UNKNOWN_ERROR in response


def trajectory_has_kb_query_error(trajectory: Any) -> bool:
    if not isinstance(trajectory, list) or not trajectory:
        return False
    count = 0
    for step in trajectory:
        if not isinstance(step, dict):
            continue
        obs = step.get("obs", "")
        if isinstance(obs, str) and KB_QUERY_ERROR in obs:
            count += 1
            if count >= TRAJECTORY_RERUN_OCCURRENCE_THRESHOLD_STRICT:
                return True
    return False


def trajectory_has_sql_quoting_bug_obs(trajectory: Any) -> bool:
    if not isinstance(trajectory, list):
        return False
    for step in trajectory:
        if not isinstance(step, dict):
            continue
        act = step.get("act", "")
        obs = step.get("obs", "")
        if not isinstance(act, str) or not isinstance(obs, str):
            continue
        tool = act.split(None, 1)[0] if act.strip() else ""
        for marker_tool, marker_obs in SQL_QUOTING_BUG_MARKERS:
            if tool == marker_tool and obs.startswith(marker_obs):
                return True
    return False


def trajectory_needs_rerun(trajectory: list[dict[str, str]]) -> bool:
    return (
        trajectory_has_timeout_obs(trajectory)
        or trajectory_has_hiccup_obs(trajectory)
        or trajectory_has_env_died_obs(trajectory)
        or trajectory_has_unknown_error(trajectory)
        or trajectory_has_kb_query_error(trajectory)
        or trajectory_has_sql_quoting_bug_obs(trajectory)
    )


def summarize_rows(
    rows: list[dict[str, Any]],
    include_partial: bool,
    expected_passes: int,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Author-compatible HiL-Bench summarize_rows port.

    This mirrors upstream run_hil_bench.py:summarize_rows: rows are first grouped
    by task/model/mode, infra_error rows and rerun-needed trajectories are
    excluded, and pass@k denominators condition on tasks with at least k valid
    passes.
    """
    grouped_attempt_rows: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped_attempt_rows[(str(row["task_name"]), str(row["model"]), str(row["mode"]))].append(row)

    grouped_mode_model_attempts: dict[tuple[str, str], list[list[dict[str, Any]]]] = defaultdict(list)
    for (_task_name, model, mode), attempt_rows in grouped_attempt_rows.items():
        trajectory_cache: dict[str, list[dict[str, str]]] = {}
        valid_passes: list[dict[str, Any]] = []
        for row in sorted(attempt_rows, key=lambda item: int(item.get("pass_num", 0))):
            if row.get("status") == "infra_error":
                continue
            trajectory_dir = str(row.get("trajectory_dir") or "")
            if trajectory_dir not in trajectory_cache:
                trajectory_cache[trajectory_dir] = load_normalized_trajectory_steps_from_dir(trajectory_dir)
            if trajectory_needs_rerun(trajectory_cache[trajectory_dir]):
                continue
            valid_passes.append(row)
        num_valid = len(valid_passes)
        should_include = num_valid >= 1 if include_partial else num_valid >= expected_passes
        if should_include:
            grouped_mode_model_attempts[(mode, model)].append(valid_passes)

    finalized: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for (mode, model), attempt_passes in grouped_mode_model_attempts.items():
        num_solved_by_pass_k = {k: 0 for k in range(1, expected_passes + 1)}
        num_attempts_with_k_passes = {k: 0 for k in range(1, expected_passes + 1)}
        total_attempts_and_passes = 0
        total_cost = 0.0
        total_steps = 0.0
        total_tokens_sent = 0.0
        total_tokens_received = 0.0
        total_questions = 0.0
        total_blockers_resolved = 0.0
        total_blockers_present = 0.0
        for valid_passes in attempt_passes:
            num_valid = len(valid_passes)
            for k in range(1, expected_passes + 1):
                if num_valid >= k:
                    num_attempts_with_k_passes[k] += 1
            for k in range(1, num_valid + 1):
                if any(bool(valid_passes[i].get("resolved")) for i in range(k)):
                    num_solved_by_pass_k[k] += 1
            for row in valid_passes:
                total_attempts_and_passes += 1
                total_cost += float(row.get("cost") or 0.0)
                total_steps += float(row.get("num_steps") or 0.0)
                total_tokens_sent += float(row.get("tokens_sent") or 0.0)
                total_tokens_received += float(row.get("tokens_received") or 0.0)
                total_questions += float(row.get("num_questions") or 0.0)
                if mode == "ask_human":
                    total_blockers_resolved += float(row.get("num_blockers_resolved") or 0.0)
                    total_blockers_present += float(row.get("total_num_blockers") or 0.0)

        metrics: dict[str, Any] = {
            "num_included_attempts": len(attempt_passes),
            "num_passes": expected_passes,
            "total_attempts_and_passes": total_attempts_and_passes,
            "avg_cost_per_pass": (total_cost / total_attempts_and_passes) if total_attempts_and_passes > 0 else 0.0,
            "avg_steps_per_pass": (total_steps / total_attempts_and_passes) if total_attempts_and_passes > 0 else 0.0,
            "avg_tokens_sent_per_pass": (total_tokens_sent / total_attempts_and_passes) if total_attempts_and_passes > 0 else 0.0,
            "avg_tokens_received_per_pass": (
                total_tokens_received / total_attempts_and_passes if total_attempts_and_passes > 0 else 0.0
            ),
            "avg_tokens_total_per_pass": (
                (total_tokens_sent + total_tokens_received) / total_attempts_and_passes
                if total_attempts_and_passes > 0
                else 0.0
            ),
        }
        for k in range(1, expected_passes + 1):
            denominator = num_attempts_with_k_passes[k]
            metrics[f"pass_at_{k}"] = num_solved_by_pass_k[k] / denominator if denominator > 0 else 0.0
            metrics[f"pass_at_{k}_n"] = denominator
        if mode == "ask_human":
            ask_precision = total_blockers_resolved / total_questions if total_questions > 0 else 0.0
            ask_recall = total_blockers_resolved / total_blockers_present if total_blockers_present > 0 else 0.0
            ask_f1 = (
                2 * ask_precision * ask_recall / (ask_precision + ask_recall)
                if (ask_precision + ask_recall) > 0
                else 0.0
            )
            metrics["ask_precision"] = ask_precision
            metrics["ask_recall"] = ask_recall
            metrics["ask_f1"] = ask_f1
            metrics["avg_num_questions_per_pass"] = (
                total_questions / total_attempts_and_passes if total_attempts_and_passes > 0 else 0.0
            )
        finalized[mode][model] = metrics
    return dict(finalized)


def compute_passk(by_instance: dict[str, list[dict[str, Any]]], requested_k: list[int]) -> dict[str, Any]:
    total = len(by_instance)
    aligned_instances = sum(
        1 for attempts in by_instance.values() if attempts and attempts[0].get("hil_evaluator_aligned") is True
    )
    metrics: dict[str, Any] = {
        "total_instances": total,
        "instances": by_instance,
        "pass_at_k": {},
        "unbiased_pass_at_k": {},
        "swebench_pro_test_pass_at_k": {},
        "unbiased_swebench_pro_test_pass_at_k": {},
        "hil_evaluator_coverage": aligned_instances / total if total else 0.0,
        "hil_evaluator_aligned_instances": aligned_instances,
        "hil_evaluator_missing_aligned_instances": total - aligned_instances,
    }
    for k in sorted(set(requested_k)):
        solved = 0
        swebench_pro_test_solved = 0
        unbiased_sum = 0.0
        unbiased_count = 0
        swebench_pro_unbiased_sum = 0.0
        swebench_pro_unbiased_count = 0
        for attempts in by_instance.values():
            first_k = attempts[:k]
            if any(item["resolved"] is True for item in first_k):
                solved += 1
            if any(item.get("swebench_pro_test_resolved") is True for item in first_k):
                swebench_pro_test_solved += 1
            n = len(attempts)
            c = sum(1 for item in attempts if item["resolved"] is True)
            if n >= k:
                unbiased_count += 1
                unbiased_sum += unbiased_estimate(n, c, k)
                swebench_pro_unbiased_count += 1
                swebench_pro_c = sum(1 for item in attempts if item.get("swebench_pro_test_resolved") is True)
                swebench_pro_unbiased_sum += unbiased_estimate(n, swebench_pro_c, k)
        metrics["pass_at_k"][str(k)] = solved / total if total else 0.0
        metrics["unbiased_pass_at_k"][str(k)] = unbiased_sum / unbiased_count if unbiased_count else None
        metrics["swebench_pro_test_pass_at_k"][str(k)] = swebench_pro_test_solved / total if total else 0.0
        metrics["unbiased_swebench_pro_test_pass_at_k"][str(k)] = (
            swebench_pro_unbiased_sum / swebench_pro_unbiased_count if swebench_pro_unbiased_count else None
        )
    metrics["missing_eval_attempts"] = sum(1 for attempts in by_instance.values() for item in attempts if item["eval_missing"])
    metrics["missing_hil_aligned_eval_attempts"] = sum(
        1 for attempts in by_instance.values() for item in attempts if item.get("hil_evaluator_aligned") is not True
    )
    metrics["swebench_pro_test_pass_count"] = sum(
        1
        for attempts in by_instance.values()
        for item in attempts
        if item.get("swebench_pro_test_resolved") is True
    )
    metrics["ungrounded_or_underconstrained_test_pass_count"] = sum(
        1
        for attempts in by_instance.values()
        for item in attempts
        if item.get("swebench_pro_test_resolved") is True and item.get("resolved") is not True
    )
    return metrics
