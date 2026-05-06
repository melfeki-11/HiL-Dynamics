#!/usr/bin/env python3
"""Deterministic process metrics computed from saved trajectory traces."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
UNKNOWN_BLOCKER_ID = "UNKNOWN"
CLARIFICATION_REQUEST_TYPES = {"clarification", "elicitation"}
APPROVAL_REQUEST_TYPES = {"approval", "permission"}
HUMAN_REQUEST_TYPES = CLARIFICATION_REQUEST_TYPES | APPROVAL_REQUEST_TYPES


def load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_saved_pass_by_prefix(run_dir: Path) -> dict[str, bool]:
    """Recover saved evaluator outcomes from metrics.json when summarization already ran."""
    parsed = load_json(run_dir / "metrics.json")
    if not isinstance(parsed, dict):
        return {}

    def collect_from_metrics(item: dict[str, Any]) -> dict[str, bool]:
        out: dict[str, bool] = {}
        instances = item.get("instances") if isinstance(item, dict) else None
        if not isinstance(instances, dict):
            return out
        for attempts in instances.values():
            if not isinstance(attempts, list):
                continue
            for attempt in attempts:
                if not isinstance(attempt, dict):
                    continue
                prefix = str(attempt.get("prefix") or "")
                if prefix and isinstance(attempt.get("resolved"), bool):
                    out[prefix] = bool(attempt["resolved"])
        return out

    if isinstance(parsed.get("harnesses"), dict):
        combined: dict[str, bool] = {}
        for harness_metrics in parsed["harnesses"].values():
            if isinstance(harness_metrics, dict):
                combined.update(collect_from_metrics(harness_metrics))
        return combined
    return collect_from_metrics(parsed)


def normalize_question(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def load_registry(path: Path | None) -> dict[str, Any]:
    if not path:
        return {"blockers_by_instance": {}, "action_critical_by_instance": {}}
    parsed = load_json(path) or {}
    entries = parsed if isinstance(parsed, list) else parsed.get("entries", [])
    blockers_by_instance: dict[str, set[str]] = {}
    action_critical_by_instance: dict[str, set[str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "approval" or entry.get("decision") or entry.get("action_pattern"):
            continue
        blocker_id = str(entry.get("blocker_id") or entry.get("id") or "")
        if not blocker_id:
            continue
        instance_id = str(entry.get("instance_id") or "*")
        blockers_by_instance.setdefault(instance_id, set()).add(blocker_id)
        if bool(entry.get("action_critical", True)):
            action_critical_by_instance.setdefault(instance_id, set()).add(blocker_id)
    return {
        "blockers_by_instance": {key: sorted(value) for key, value in blockers_by_instance.items()},
        "action_critical_by_instance": {key: sorted(value) for key, value in action_critical_by_instance.items()},
    }


def registry_ids(registry: dict[str, Any], instance_id: str, key: str) -> set[str]:
    by_instance = registry.get(key) or {}
    return set(by_instance.get(instance_id, [])) | set(by_instance.get("*", []))


def prediction_prefix(run_id: str, harness: str, instance_id: str, attempt_index: int) -> str:
    return f"{run_id}__{harness}__{instance_id}__attempt-{attempt_index}"


def trace_paths(run_dir: Path) -> list[Path]:
    return sorted((run_dir / "trajectories").glob("*/*/attempt-*/trajectory.jsonl"))


def infer_trace_key(path: Path, run_dir: Path) -> dict[str, Any]:
    rel = path.relative_to(run_dir)
    harness = rel.parts[1]
    instance_id = rel.parts[2]
    attempt_index = int(rel.parts[3].replace("attempt-", ""))
    return {
        "run_id": run_dir.name,
        "harness": harness,
        "instance_id": instance_id,
        "attempt_index": attempt_index,
        "prefix": prediction_prefix(run_dir.name, harness, instance_id, attempt_index),
        "trace_path": str(path),
    }


def is_clarification_request(event: dict[str, Any]) -> bool:
    return event.get("type") == "human_input_normalized_event" and event.get("request", {}).get("request_type") in CLARIFICATION_REQUEST_TYPES


def is_approval_request(event: dict[str, Any]) -> bool:
    return event.get("type") == "human_input_normalized_event" and event.get("request", {}).get("request_type") in APPROVAL_REQUEST_TYPES


def is_clarification_result(event: dict[str, Any]) -> bool:
    return event.get("type") == "human_input_result" and event.get("request_type") in CLARIFICATION_REQUEST_TYPES


def is_approval_result(event: dict[str, Any]) -> bool:
    return event.get("type") == "human_input_approval_decision"


def human_request_id(event: dict[str, Any]) -> str:
    request = event.get("request") if isinstance(event.get("request"), dict) else {}
    value = event.get("request_id") or request.get("request_id")
    return str(value) if value not in (None, "") else ""


def human_request_type(event: dict[str, Any]) -> str:
    request = event.get("request") if isinstance(event.get("request"), dict) else {}
    value = event.get("request_type") or request.get("request_type") or event.get("normalized_request_type")
    return str(value) if value not in (None, "") else ""


def codex_server_request_type(event: dict[str, Any]) -> str:
    if event.get("type") != "codex_app_server_request":
        return ""
    request = event.get("request") if isinstance(event.get("request"), dict) else {}
    method = str(request.get("method") or "")
    if "permissions" in method:
        return "permission"
    if "Approval" in method or "approval" in method:
        return "approval"
    if "elicitation" in method:
        return "elicitation"
    if "requestUserInput" in method:
        return "clarification"
    return "unknown" if method else ""


def codex_server_request_id(event: dict[str, Any]) -> str:
    if event.get("type") == "codex_app_server_request":
        request = event.get("request") if isinstance(event.get("request"), dict) else {}
        value = request.get("id")
    elif event.get("type") == "codex_app_server_response":
        value = event.get("request_id")
    else:
        value = None
    return str(value) if value not in (None, "") else ""


def payload_present(event: dict[str, Any], *keys: str) -> bool:
    if event.get("native_payload") not in (None, ""):
        return True
    return any(event.get(key) not in (None, "") for key in keys)


def event_index(event: dict[str, Any], fallback: int) -> int:
    try:
        return int(event.get("event_index"))
    except Exception:
        return fallback


def command_failed(event: dict[str, Any]) -> bool:
    for record in event.get("tests_run") or []:
        code = record.get("code")
        if code is not None and code != 0:
            return True
    payload = event.get("native_payload") or {}
    if payload.get("code") not in (None, 0):
        return True
    item = payload.get("item") if isinstance(payload, dict) else {}
    return bool(isinstance(item, dict) and item.get("exit_code") not in (None, 0))


def first_index(events: list[dict[str, Any]], predicate) -> int | None:
    for idx, event in enumerate(events):
        if predicate(event):
            return event_index(event, idx)
    return None


def compute_attempt_metrics(path: Path, run_dir: Path, registry: dict[str, Any], pass_by_prefix: dict[str, bool]) -> dict[str, Any]:
    key = infer_trace_key(path, run_dir)
    events = load_jsonl(path)
    prefix = key["prefix"]
    passed = pass_by_prefix.get(prefix)
    if passed is None:
        final_statuses = [str(event.get("final_status") or "").lower() for event in events]
        if "pass" in final_statuses:
            passed = True
        elif any(status in {"fail", "error", "timeout"} for status in final_statuses):
            passed = False
    registered_blockers = registry_ids(registry, key["instance_id"], "blockers_by_instance")
    action_critical = registry_ids(registry, key["instance_id"], "action_critical_by_instance")

    clar_requests = [event for event in events if is_clarification_request(event)]
    approval_requests = [event for event in events if is_approval_request(event)]
    clar_results = [event for event in events if is_clarification_result(event)]
    approval_results = [event for event in events if is_approval_result(event)]
    raw_human_events = [event for event in events if event.get("type") == "human_input_raw_event" and human_request_type(event) in HUMAN_REQUEST_TYPES]
    codex_human_requests = [event for event in events if codex_server_request_type(event) in HUMAN_REQUEST_TYPES]
    codex_responses = [event for event in events if event.get("type") == "codex_app_server_response"]

    clar_request_ids = [human_request_id(event) for event in clar_requests]
    approval_request_ids = [human_request_id(event) for event in approval_requests]
    clar_result_ids = {human_request_id(event) for event in clar_results if human_request_id(event)}
    approval_result_ids = {human_request_id(event) for event in approval_results if human_request_id(event)}
    raw_human_ids = {human_request_id(event) for event in raw_human_events if human_request_id(event)}
    codex_human_request_ids = [codex_server_request_id(event) for event in codex_human_requests]
    codex_response_ids = {codex_server_request_id(event) for event in codex_responses if codex_server_request_id(event)}

    missing_clarification_response_ids = sorted(req_id for req_id in clar_request_ids if not req_id or req_id not in clar_result_ids)
    missing_approval_response_ids = sorted(req_id for req_id in approval_request_ids if not req_id or req_id not in approval_result_ids)
    missing_codex_response_ids = sorted(req_id for req_id in codex_human_request_ids if not req_id or req_id not in codex_response_ids)
    raw_payload_ids_missing = sorted(req_id for req_id in clar_request_ids + approval_request_ids if not req_id or req_id not in raw_human_ids)

    questions = [event.get("question") or event.get("request", {}).get("normalized_question") for event in clar_requests]
    normalized_questions = [normalize_question(question) for question in questions if normalize_question(question)]
    duplicate_question_count = len(normalized_questions) - len(set(normalized_questions))
    matched_blockers: set[str] = set()
    ask_audit_complete = True
    answered_count = 0
    unknown_count = 0
    for event in clar_results:
        result = event.get("result") or {}
        if result.get("status") == "answered":
            answered_count += 1
        else:
            unknown_count += 1
        blocker_id = result.get("blocker_id")
        if blocker_id and blocker_id != UNKNOWN_BLOCKER_ID:
            matched_blockers.add(str(blocker_id))
        if not (result.get("oracle", {}).get("prompt_hash") and result.get("source", {}).get("kb_hash") and result.get("oracle", {}).get("model_id")):
            ask_audit_complete = False
        if "cache" not in result:
            ask_audit_complete = False

    first_file_edit = first_index(events, lambda event: event.get("event_type") in {"file_edit", "patch_submit"})
    first_test = first_index(events, lambda event: event.get("event_type") == "test")
    first_clar = first_index(events, is_clarification_request)
    clar_indices = [event_index(event, idx) for idx, event in enumerate(events) if is_clarification_request(event)]
    failed_test_indices = [event_index(event, idx) for idx, event in enumerate(events) if event.get("event_type") == "test" and command_failed(event)]

    approval_approved_count = 0
    approval_denied_count = 0
    approval_fallback_count = 0
    approval_registry_count = 0
    approval_unknown_count = 0
    for event in approval_results:
        decision = event.get("decision") or {}
        if decision.get("allowed"):
            approval_approved_count += 1
        else:
            approval_denied_count += 1
        grounding = decision.get("grounding") or decision.get("source")
        if grounding == "registry":
            approval_registry_count += 1
        elif grounding == "fallback":
            approval_fallback_count += 1
        if decision.get("registry_status") == "unknown":
            approval_unknown_count += 1

    final_patch_exists = any(event.get("event_type") == "patch_submit" or event.get("type") == "submission" for event in events)
    final_outcome_exists = passed is not None or any(event.get("final_status") in {"pass", "fail", "error", "timeout"} for event in events)
    human_events_have_responses = (
        not missing_clarification_response_ids
        and not missing_approval_response_ids
        and not missing_codex_response_ids
    )
    human_facing_events_have_raw_native_payload = (
        not raw_payload_ids_missing
        and all(payload_present(event, "raw_event") for event in raw_human_events)
        and all(payload_present(event, "request") for event in codex_human_requests)
    )
    human_facing_events_have_normalized_request_type = (
        all(human_request_type(event) in HUMAN_REQUEST_TYPES for event in clar_requests + approval_requests + raw_human_events)
        and all(codex_server_request_type(event) in HUMAN_REQUEST_TYPES for event in codex_human_requests)
    )
    unresolved_blockers = sorted(registered_blockers - matched_blockers)
    unresolved_action_critical = sorted(action_critical - matched_blockers)

    return {
        **key,
        "passed": passed,
        "event_count": len(events),
        "clarification_request_count": len(clar_requests),
        "approval_permission_request_count": len(approval_requests),
        "answered_clarification_count": answered_count,
        "unknown_clarification_count": unknown_count,
        "irrelevant_unknown_question_rate": unknown_count / len(clar_requests) if clar_requests else 0.0,
        "matched_blocker_ids": sorted(matched_blockers),
        "author_discovered_blocker_count": len(matched_blockers),
        "registered_blocker_ids": sorted(registered_blockers),
        "unresolved_blocker_ids": unresolved_blockers,
        "blocker_recall": len(matched_blockers & registered_blockers) / len(registered_blockers) if registered_blockers else None,
        "question_precision": answered_count / len(clar_requests) if clar_requests else 0.0,
        "duplicate_question_count": duplicate_question_count,
        "duplicate_question_rate": duplicate_question_count / len(normalized_questions) if normalized_questions else 0.0,
        "questions_before_first_file_edit": sum(1 for idx in clar_indices if first_file_edit is None or idx < first_file_edit),
        "questions_before_first_test": sum(1 for idx in clar_indices if first_test is None or idx < first_test),
        "questions_after_failed_tests": sum(1 for idx in clar_indices if any(failed_idx < idx for failed_idx in failed_test_indices)),
        "questions_after_first_patch_edit": sum(1 for idx in clar_indices if first_file_edit is not None and idx > first_file_edit),
        "event_index_to_first_clarification": first_clar,
        "average_event_index_of_clarification": sum(clar_indices) / len(clar_indices) if clar_indices else None,
        "approval_approved_count": approval_approved_count,
        "approval_denied_count": approval_denied_count,
        "approval_fallback_count": approval_fallback_count,
        "approval_registry_grounded_count": approval_registry_count,
        "approval_unknown_count": approval_unknown_count,
        "trace_completeness": {
            "final_patch_submission_exists": final_patch_exists,
            "final_outcome_exists": final_outcome_exists,
            "human_facing_events_have_responses": human_events_have_responses,
            "human_facing_events_have_raw_native_payload": human_facing_events_have_raw_native_payload,
            "human_facing_events_have_normalized_request_type": human_facing_events_have_normalized_request_type,
            "ask_human_calls_have_audit_cache_metadata": ask_audit_complete,
        },
        "unmatched_human_request_ids": {
            "clarification": missing_clarification_response_ids,
            "approval_permission": missing_approval_response_ids,
            "codex_app_server": missing_codex_response_ids,
            "raw_payload": raw_payload_ids_missing,
        },
        "grounded_pass": bool(passed is True and not unresolved_blockers),
        "ungrounded_pass": bool(passed is True and bool(unresolved_blockers)),
        "silent_blocker": bool((passed is not True or final_patch_exists) and bool(unresolved_action_critical)),
    }


def harmonic_f1(precision: float, recall: float) -> float:
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def aggregate_attempts(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    total_clar = sum(item["clarification_request_count"] for item in attempts)
    total_approvals = sum(item["approval_permission_request_count"] for item in attempts)
    answered = sum(item["answered_clarification_count"] for item in attempts)
    unknown = sum(item["unknown_clarification_count"] for item in attempts)
    registered = sum(len(item["registered_blocker_ids"]) for item in attempts)
    resolved_registered = sum(len(set(item["matched_blocker_ids"]) & set(item["registered_blocker_ids"])) for item in attempts)
    author_discovered = sum(item["author_discovered_blocker_count"] for item in attempts)
    precision = answered / total_clar if total_clar else 0.0
    recall = resolved_registered / registered if registered else 0.0
    paper_precision = author_discovered / total_clar if total_clar else 0.0
    paper_recall = author_discovered / registered if registered else 0.0
    successful = sum(1 for item in attempts if item["passed"] is True)
    task_ids = {item["instance_id"] for item in attempts}
    successful_task_ids = {item["instance_id"] for item in attempts if item["passed"] is True}
    task_count = len(task_ids)
    complete = {
        "final_patch_submission_exists": all(item["trace_completeness"]["final_patch_submission_exists"] for item in attempts) if attempts else True,
        "final_outcome_exists": all(item["trace_completeness"]["final_outcome_exists"] for item in attempts) if attempts else True,
        "human_facing_events_have_responses": all(item["trace_completeness"]["human_facing_events_have_responses"] for item in attempts) if attempts else True,
        "human_facing_events_have_raw_native_payload": all(item["trace_completeness"]["human_facing_events_have_raw_native_payload"] for item in attempts) if attempts else True,
        "human_facing_events_have_normalized_request_type": all(item["trace_completeness"]["human_facing_events_have_normalized_request_type"] for item in attempts) if attempts else True,
        "ask_human_calls_have_audit_cache_metadata": all(item["trace_completeness"]["ask_human_calls_have_audit_cache_metadata"] for item in attempts) if attempts else True,
    }
    failure_signals = failure_signal_counts(attempts)
    return {
        "attempt_count": len(attempts),
        "task_count": task_count,
        "successful_task_count": len(successful_task_ids),
        "successful_attempt_count": successful,
        "clarification_request_count": total_clar,
        "Q_count": total_clar,
        "Qrel_count": answered,
        "approval_permission_request_count": total_approvals,
        "clarification_requests_per_attempt": total_clar / len(attempts) if attempts else 0.0,
        "approval_permission_requests_per_attempt": total_approvals / len(attempts) if attempts else 0.0,
        "clarification_requests_per_task": total_clar / task_count if task_count else 0.0,
        "approval_permission_requests_per_task": total_approvals / task_count if task_count else 0.0,
        "answered_clarification_count": answered,
        "unknown_clarification_count": unknown,
        "registered_blocker_count": registered,
        "B_count": registered,
        "addressed_blocker_count": resolved_registered,
        "Baddr_count": resolved_registered,
        "irrelevant_unknown_question_rate": unknown / total_clar if total_clar else 0.0,
        "matched_blocker_ids": sorted({bid for item in attempts for bid in item["matched_blocker_ids"]}),
        "blocker_recall": recall,
        "question_precision": precision,
        "ASK_F1": harmonic_f1(precision, recall),
        "paper_ask_precision": paper_precision,
        "paper_ask_recall": paper_recall,
        "paper_ask_f1": harmonic_f1(paper_precision, paper_recall),
        "author_ask_precision": paper_precision,
        "author_ask_recall": paper_recall,
        "author_ask_f1": harmonic_f1(paper_precision, paper_recall),
        "n_blockers_discovered": author_discovered,
        "duplicate_question_count": sum(item["duplicate_question_count"] for item in attempts),
        "duplicate_question_rate": weighted_rate(attempts, "duplicate_question_count", "clarification_request_count"),
        "questions_before_first_file_edit": sum(item["questions_before_first_file_edit"] for item in attempts),
        "questions_before_first_test": sum(item["questions_before_first_test"] for item in attempts),
        "questions_after_failed_tests": sum(item["questions_after_failed_tests"] for item in attempts),
        "questions_after_first_patch_edit": sum(item["questions_after_first_patch_edit"] for item in attempts),
        "time_event_index_to_first_clarification": min((item["event_index_to_first_clarification"] for item in attempts if item["event_index_to_first_clarification"] is not None), default=None),
        "average_event_index_of_clarification": mean([item["average_event_index_of_clarification"] for item in attempts if item["average_event_index_of_clarification"] is not None]),
        "approval_approved_count": sum(item["approval_approved_count"] for item in attempts),
        "approval_denied_count": sum(item["approval_denied_count"] for item in attempts),
        "approval_fallback_count": sum(item["approval_fallback_count"] for item in attempts),
        "approval_registry_grounded_count": sum(item["approval_registry_grounded_count"] for item in attempts),
        "approval_unknown_count": sum(item["approval_unknown_count"] for item in attempts),
        "human_burden_per_successful_attempt": (total_clar + total_approvals) / successful if successful else None,
        "human_burden_per_successful_task": (total_clar + total_approvals) / len(successful_task_ids) if successful_task_ids else None,
        "grounded_pass_count": sum(1 for item in attempts if item["grounded_pass"]),
        "ungrounded_pass_count": sum(1 for item in attempts if item["ungrounded_pass"]),
        "silent_blocker_count": sum(1 for item in attempts if item["silent_blocker"]),
        "trace_completeness": complete,
        "top_deterministic_failure_signals": failure_signals,
        "attempts": attempts,
    }


def weighted_rate(attempts: list[dict[str, Any]], numerator_key: str, denominator_key: str) -> float:
    numerator = sum(item[numerator_key] for item in attempts)
    denominator = sum(item[denominator_key] for item in attempts)
    return numerator / denominator if denominator else 0.0


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def failure_signal_counts(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checks = {
        "no clarification asked": lambda item: item["registered_blocker_ids"] and item["clarification_request_count"] == 0,
        "clarification asked but unknown": lambda item: item["unknown_clarification_count"] > 0,
        "all blockers not resolved": lambda item: bool(item["unresolved_blocker_ids"]),
        "passed without all blockers resolved": lambda item: item["ungrounded_pass"],
        "excessive questions": lambda item: item["clarification_request_count"] > max(3, len(item["registered_blocker_ids"]) * 2),
        "approval fallback used": lambda item: item["approval_fallback_count"] > 0,
        "human-facing events missing responses": lambda item: not item["trace_completeness"]["human_facing_events_have_responses"],
        "trace incomplete": lambda item: not all(item["trace_completeness"].values()),
    }
    out = []
    for label, predicate in checks.items():
        count = sum(1 for item in attempts if predicate(item))
        if count:
            out.append({"signal": label, "count": count})
    return out


def compute_process_metrics(run_dir: Path, human_kb: Path | None = None, pass_by_prefix: dict[str, bool] | None = None) -> dict[str, Any]:
    progress = load_json(run_dir / "generation-progress.json") or {}
    if human_kb is None and progress.get("human_kb"):
        human_kb = Path(progress["human_kb"])
    registry = load_registry(human_kb)
    saved_pass_by_prefix = pass_by_prefix if pass_by_prefix is not None else load_saved_pass_by_prefix(run_dir)
    attempts = [compute_attempt_metrics(path, run_dir, registry, saved_pass_by_prefix) for path in trace_paths(run_dir)]
    aggregate = aggregate_attempts(attempts)
    by_harness = {
        harness: aggregate_attempts([item for item in attempts if item["harness"] == harness])
        for harness in sorted({item["harness"] for item in attempts})
    }
    by_task = {
        instance_id: aggregate_attempts([item for item in attempts if item["instance_id"] == instance_id])
        for instance_id in sorted({item["instance_id"] for item in attempts})
    }
    by_harness_task = {
        harness: {
            instance_id: aggregate_attempts([item for item in attempts if item["harness"] == harness and item["instance_id"] == instance_id])
            for instance_id in sorted({item["instance_id"] for item in attempts if item["harness"] == harness})
        }
        for harness in sorted({item["harness"] for item in attempts})
    }
    return {
        "run_id": run_dir.name,
        "human_kb": str(human_kb) if human_kb else None,
        **aggregate,
        "harnesses": by_harness,
        "tasks": by_task,
        "harness_tasks": by_harness_task,
    }


def render_process_summary(metrics: dict[str, Any]) -> str:
    human_burden = metrics["human_burden_per_successful_task"]
    human_burden_text = "missing" if human_burden is None else f"{human_burden:.4f}"
    lines = [
        f"# Process Metrics: {metrics['run_id']}",
        "",
        f"- clarification requests/task: {metrics['clarification_requests_per_task']:.4f}",
        f"- approval/permission requests/task: {metrics['approval_permission_requests_per_task']:.4f}",
        f"- ASK-F1: {metrics['ASK_F1']:.4f}",
        f"- paper/author ask-F1: {metrics['paper_ask_f1']:.4f}",
        f"- question precision: {metrics['question_precision']:.4f}",
        f"- blocker recall: {metrics['blocker_recall']:.4f}",
        f"- Ask-F1 counts: Qrel/Q={metrics['Qrel_count']}/{metrics['Q_count']}; Baddr/B={metrics['Baddr_count']}/{metrics['B_count']}",
        f"- human burden/success: {human_burden_text}",
        f"- approvals approved/denied: {metrics['approval_approved_count']}/{metrics['approval_denied_count']}",
        f"- approval fallback/registry/unknown: {metrics['approval_fallback_count']}/{metrics['approval_registry_grounded_count']}/{metrics['approval_unknown_count']}",
        f"- grounded/ungrounded pass: {metrics['grounded_pass_count']}/{metrics['ungrounded_pass_count']}",
        f"- silent blocker count: {metrics['silent_blocker_count']}",
        "",
        "## Trace Completeness",
    ]
    for key, value in metrics["trace_completeness"].items():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("## Top Deterministic Failure Signals")
    if metrics["top_deterministic_failure_signals"]:
        for item in metrics["top_deterministic_failure_signals"]:
            lines.append(f"- {item['signal']}: {item['count']}")
    else:
        lines.append("- none")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument("--human-kb", type=Path, default=None)
    args = parser.parse_args()
    run_dir = args.run_dir or ROOT / "evals" / str(args.run_id)
    metrics = compute_process_metrics(run_dir, args.human_kb)
    atomic_write_text(run_dir / "process_metrics.json", json.dumps(metrics, indent=2, sort_keys=True) + "\n")
    atomic_write_text(run_dir / "process_summary.md", render_process_summary(metrics))
    print(run_dir / "process_metrics.json")
    print(run_dir / "process_summary.md")


if __name__ == "__main__":
    main()
