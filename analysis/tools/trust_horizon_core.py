"""Trust Horizon v0 analysis helpers for HIL-Bench trajectories."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

DEFAULT_HARNESS = "hil_bench"
CONDITION_ROLES = {
    "full_info": "full_info",
    "full": "full_info",
    "unblocked": "full_info",
    "blocked_no_human": "blocked",
    "no_human": "blocked",
    "no_ask": "blocked",
    "blocked": "blocked",
    "blocked_with_ask": "ask",
    "ask_human": "ask",
    "ask": "ask",
}

TEST_RE = re.compile(
    r"\b(pytest|mocha|go test|go build|go vet|npm test|yarn test|pnpm test|make test|tox|cargo test|"
    r"jest|npx\s+jest|tsc\s+--noEmit|npx\s+tsc|py_compile|compileall|"
    r"yarn\s+workspace\s+\S+\s+(test|check-types|eslint)|prettier\s+--check)\b",
    re.I,
)
ASK_RE = re.compile(r"\bask_human(?:\b|_)", re.I)
VIEW_RE = re.compile(r"\bstr_replace_editor\s+(?:\$?[\"'])?view\b", re.I)
WRITE_RE = re.compile(
    r"^\s*(edit|write):\s*\{|"
    r"\b(str_replace_editor\s+(?!\$?[\"']?view\b)|apply_patch|cat\s+<<|cat\s+>|echo\s+.*>|sed\s+-i|tee\s+|cp\s+|mv\s+|"
    r"prettier\s+--write|python3?\s+.*(?:open\(|write_text\(|write_bytes\())",
    re.I | re.S,
)
SUBMIT_RE = re.compile(r"^\s*submit\b", re.I | re.M)
GIT_RE = re.compile(r"(^|\b)(git(?:\s+-C\s+\S+)?\s+(diff|status|show|log|grep|ls-files)|gh\s+)", re.I)
READ_CMD_RE = re.compile(
    r"^\s*(read|glob)(:|\s)|(^|\b)(find|grep|rg|ls|sed\s+-n|cat|head|tail|wc|tree|pwd)\b|"
    r"\bread_text\(|Path\([^)]+\)\.read_text\(",
    re.I | re.S,
)
EXECUTE_RE = re.compile(r"(^|\b)(python3?\s+-\s*<<|cmp\s+-s)", re.I | re.S)
BASH_TOOL_RE = re.compile(r"^\s*Bash:\s*(\{.*\})\s*$", re.I | re.S)
DIRECT_READ_RE = re.compile(
    r"^\s*(Read|WebFetch)(:|\s)|\bstr_replace_editor\s+(?:\$?[\"'])?view\b|"
    r"(^|\b)(cat|sed\s+-n|head|tail)\s+\S+",
    re.I | re.S,
)
BROAD_INVENTORY_RE = re.compile(
    r"^\s*(ls|tree)\b|^\s*rg\s+--files\b|^\s*find\s+\S*\s*-type\s+f\b|"
    r"^\s*Glob(:|\s)\s*\{[^}]*\*\*/\*",
    re.I | re.S,
)
TARGETED_LOOKUP_RE = re.compile(
    r"^\s*(Grep)(:|\s)|^\s*rg\s+(?!--files\b).+|^\s*grep\s+.+|"
    r"^\s*find\s+.+\s-name\s+.+|^\s*find\s+.+\|\s*grep\s+.+|"
    r"^\s*Glob(:|\s)\s*\{[^}]*[A-Za-z0-9_][^}]*\}",
    re.I | re.S,
)
HARD_TEST_FAIL_RE = re.compile(
    r"(^|\n)\s*(--- FAIL:|FAIL\b|FAILED\b|ERROR\b)|"
    r"\b(AssertionError|Traceback|SyntaxError|TypeError|ValueError|panic:|exit status [1-9]|"
    r"build failed|Tests?:[^\n]*\bfailed\b|[1-9]\d*\s+fail(?:ed|ing)\b|not ok\b)",
    re.I,
)
TEST_PASS_RE = re.compile(
    r"(^|\n)\s*(PASS\b|PASSED\b|--- PASS:)|"
    r"\b(ok\s+\S+|\d+\s+passed\b|Tests?:[^\n]*\bpassed\b|"
    r"Test Suites?:[^\n]*\bpassed\b|all tests passed)\b|\[100%\]",
    re.I,
)
WEAK_VALIDATION_RE = re.compile(r"\b(tsc\s+--noEmit|npx\s+tsc|eslint|lint|prettier\s+--check|git\s+status|git\s+diff)\b", re.I)
VALIDATION_SETUP_FAIL_RE = re.compile(
    r"\b(command not found|no such file or directory|module not found|cannot find module|"
    r"permission denied|could not install|failed to install|network error|dependency error)\b",
    re.I,
)
TURN_LIMIT_RE = re.compile(r"\b(turn limit|time limit|timed out|timeout|maximum turns|max turns)\b", re.I)
ENVIRONMENT_ERROR_RE = re.compile(
    r"\b(tool error|internal server error|tool call failed|sandbox error|container error|"
    r"connection reset|rate limit|out of memory|no space left on device)\b",
    re.I,
)


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "t", "yes", "y", "resolved", "pass", "passed"}


def parse_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(str(value)))
    except ValueError:
        return default


def first_present(row: dict[str, Any], keys: Iterable[str], default: Any = None) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return default


def parse_condition(row: dict[str, Any], override: str | None = None) -> str:
    if override:
        return override
    for key in ("condition", "condition_role", "policy_id", "mode", "ask_behavior_group"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    s3_parts = {part for part in str(row.get("s3_key") or "").split("/") if part}
    if {"ask_human", "blocked_with_ask"} & s3_parts:
        return "blocked_with_ask"
    if {"full", "full_info", "unblocked"} & s3_parts:
        return "full_info"
    if {"no_ask", "blocked_no_human"} & s3_parts:
        return "blocked_no_human"
    return "unknown"


def condition_role(condition: str) -> str:
    return CONDITION_ROLES.get(condition, condition)


def event_type(act: str) -> str:
    act = act or ""
    # Classification is intentionally first-match: verification commands beat writes,
    # and asks beat generic shell/read patterns.
    if SUBMIT_RE.search(act):
        return "SUBMIT"
    if TEST_RE.search(act):
        return "TEST"
    if ASK_RE.search(act):
        return "ASK"
    if GIT_RE.search(act):
        return "GIT"
    if VIEW_RE.search(act):
        return "READ"
    if WRITE_RE.search(act):
        return "WRITE"
    if READ_CMD_RE.search(act):
        return "READ"
    if EXECUTE_RE.search(act):
        return "EXECUTE"
    if not act.strip():
        return "NO_ACTION"
    return "OTHER"


def action_analysis_text(act: str) -> str:
    act = act or ""
    match = BASH_TOOL_RE.match(act)
    if match:
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            return act
        command = payload.get("command")
        if isinstance(command, str) and command.strip():
            return command
    return act


def exploration_intent_class(act: str) -> str:
    text = action_analysis_text(act)
    if TEST_RE.search(text):
        return "test_probe"
    if DIRECT_READ_RE.search(text):
        return "direct_read"
    if TARGETED_LOOKUP_RE.search(text):
        return "targeted_lookup"
    if BROAD_INVENTORY_RE.search(text):
        return "broad_inventory"
    return ""


def test_outcome(obs: str) -> str:
    obs = obs or ""
    if re.search(r"\bno\s+FAILED\s+tests?\b", obs, re.I):
        return "unknown"
    if HARD_TEST_FAIL_RE.search(obs):
        return "failed"
    if TEST_PASS_RE.search(obs):
        return "passed"
    return "unknown"


def validation_status(act: str, obs: str) -> str:
    if VALIDATION_SETUP_FAIL_RE.search(obs or ""):
        return "setup_fail"
    outcome = test_outcome(obs)
    if outcome == "failed":
        return "fail"
    if outcome == "passed":
        return "weak_pass" if WEAK_VALIDATION_RE.search(act or "") else "pass"
    if WEAK_VALIDATION_RE.search(act or ""):
        return "weak_pass"
    return "unknown"


def terminal_evidence_state(
    *,
    last_verification_status: str,
    verification_after_last_write: bool,
    final_submit_present: bool,
    write_count: int,
    submit_count: int,
    turn_limit_present: bool = False,
    environment_error_present: bool = False,
) -> str:
    if environment_error_present:
        return "tool_or_environment_error"
    if turn_limit_present:
        return "turn_limit_or_timeout"
    if last_verification_status == "fail":
        return "visible_red_at_end"
    if verification_after_last_write and last_verification_status == "pass":
        return "visible_green_after_last_write"
    if verification_after_last_write and last_verification_status in {"weak_pass", "setup_fail"}:
        return "weak_validation_only"
    if write_count == 0 and submit_count == 0:
        return "no_patch_or_no_submit"
    if write_count > 0 and final_submit_present and not verification_after_last_write:
        return "unverified_patch_submitted"
    if write_count > 0 and final_submit_present and last_verification_status in {"not_run", "unknown"}:
        return "unverified_patch_submitted"
    if write_count == 0 or submit_count == 0:
        return "no_patch_or_no_submit"
    if last_verification_status in {"weak_pass", "setup_fail"}:
        return "weak_validation_only"
    return "unknown_terminal_evidence"


def ask_relevance(obs: str) -> str:
    obs_lower = " ".join((obs or "").split()).lower()
    if not obs_lower:
        return "unknown"
    if (
        obs_lower in {"irrelevant", "not relevant", "unrelated"}
        or re.search(r"\b(irrelevant|not relevant|unrelated)\s+question\b", obs_lower)
        or re.search(r"\b(question|ask)\s+(is|was)\s+(irrelevant|not relevant|unrelated)\b", obs_lower)
    ):
        return "irrelevant"
    return "relevant"


def analyze_trajectory(
    trajectory: list[dict[str, Any]],
    *,
    metadata: dict[str, Any] | None = None,
    condition: str | None = None,
) -> dict[str, Any]:
    metadata = dict(metadata or {})
    event_types: list[str] = []
    ask_seq: list[str] = []
    early_exploration_intents: list[str] = []
    first_ask_turn = 0
    first_write_turn = 0
    last_write_turn = 0
    last_submit_turn = 0
    write_count = 0
    submit_count = 0
    test_event_count = 0
    passed_test_count = 0
    failed_test_count = 0
    last_verification_command = ""
    last_verification_status = "not_run"
    last_verification_turn = 0
    turn_limit_present = False
    environment_error_present = False

    for turn, event in enumerate(trajectory, 1):
        if not isinstance(event, dict):
            event = {}
        act = str(event.get("act") or "")
        obs = str(event.get("obs") or "")
        evidence_text = f"{act}\n{obs}"
        if TURN_LIMIT_RE.search(evidence_text):
            turn_limit_present = True
        if ENVIRONMENT_ERROR_RE.search(evidence_text):
            environment_error_present = True
        kind = event_type(act)
        event_types.append(kind)
        if not first_write_turn and kind != "WRITE":
            intent = exploration_intent_class(act)
            if intent:
                early_exploration_intents.append(intent)
        if kind == "ASK":
            if not first_ask_turn:
                first_ask_turn = turn
            relevance = ask_relevance(obs)
            ask_seq.append("I" if relevance == "irrelevant" else "R" if relevance == "relevant" else "U")
        elif kind == "WRITE":
            write_count += 1
            last_write_turn = turn
            if not first_write_turn:
                first_write_turn = turn
        elif kind == "SUBMIT":
            submit_count += 1
            last_submit_turn = turn
        elif kind == "TEST":
            test_event_count += 1
            last_verification_command = " ".join(act.split())
            last_verification_status = validation_status(act, obs)
            last_verification_turn = turn
            outcome = test_outcome(obs)
            if outcome == "passed":
                passed_test_count += 1
            elif outcome == "failed":
                failed_test_count += 1

    questions_before_first_edit = 0
    if first_write_turn:
        questions_before_first_edit = sum(1 for idx, kind in enumerate(event_types, 1) if kind == "ASK" and idx < first_write_turn)
    metadata.update(
        {
            "num_turns": len(trajectory),
            "action_sequence": ",".join(event_types),
            "early_exploration_intent_sequence": ",".join(early_exploration_intents),
            "early_broad_inventory_count": early_exploration_intents.count("broad_inventory"),
            "early_targeted_lookup_count": early_exploration_intents.count("targeted_lookup"),
            "early_direct_read_count": early_exploration_intents.count("direct_read"),
            "early_test_probe_count": early_exploration_intents.count("test_probe"),
            "ask_count": len(ask_seq),
            "irrelevant_ask_count": ask_seq.count("I"),
            "relevant_ask_count": ask_seq.count("R"),
            "ask_sequence": "".join(ask_seq),
            "first_ask_turn": first_ask_turn,
            "first_write_turn": first_write_turn,
            "questions_before_first_edit": questions_before_first_edit,
            "write_count": write_count,
            "submit_count": submit_count,
            "test_event_count": test_event_count,
            "passed_test_count": passed_test_count,
            "failed_test_count": failed_test_count,
            "last_verification_command": last_verification_command,
            "last_verification_status": last_verification_status,
            "final_submit_present": bool(submit_count and last_submit_turn == len(trajectory)),
            "verification_after_last_write": bool(last_verification_turn and (not last_write_turn or last_verification_turn > last_write_turn)),
            "turn_limit_present": turn_limit_present,
            "environment_error_present": environment_error_present,
        }
    )
    return normalize_run_row(metadata, condition=condition)


def analyze_trajectory_file(
    trajectory_path: Path,
    *,
    metadata: dict[str, Any] | None = None,
    condition: str | None = None,
    out_json: Path | None = None,
) -> dict[str, Any]:
    data = json.loads(trajectory_path.read_text())
    if not isinstance(data, list):
        raise ValueError("trajectory JSON must contain a list of events")
    row = analyze_trajectory(data, metadata=metadata, condition=condition)
    if out_json is not None:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(row, indent=2, sort_keys=True) + "\n")
    return row


def normalize_run_row(row: dict[str, Any], *, condition: str | None = None) -> dict[str, Any]:
    resolved = parse_bool(row.get("resolved"))
    harness_outcome = "resolved" if resolved else "unresolved"
    if resolved:
        terminal_state = "clean_pass"
    else:
        terminal_state = (
            str(row.get("llm_terminal_state") or "").strip()
            or str(row.get("heuristic_terminal_state") or "").strip()
            or str(row.get("terminal_state") or "").strip()
            or "unknown_failure"
        )
    task_id = str(row.get("task_id") or row.get("attempt_id") or row.get("instance_id") or "").strip()
    attempt_id = str(row.get("attempt_id") or row.get("run_id") or task_id).strip()
    pass_index = parse_int(first_present(row, ("pass_index", "pass_num", "attempt_index"), 1), 1)
    normalized_condition = parse_condition(row, condition)
    action_sequence = str(row.get("action_sequence") or "")
    action_counts = Counter(part for part in action_sequence.split(",") if part)
    write_count = parse_int(first_present(row, ("write_count",), action_counts.get("WRITE", 0)))
    submit_count = parse_int(first_present(row, ("submit_count",), action_counts.get("SUBMIT", 0)))
    final_submit_present = parse_bool(row.get("final_submit_present")) or action_sequence.split(",")[-1:] == ["SUBMIT"]
    verification_after_last_write = parse_bool(row.get("verification_after_last_write"))
    last_verification_status = str(row.get("last_verification_status") or "").strip() or "not_run"
    evidence_state = str(row.get("terminal_evidence_state") or "").strip()
    if not evidence_state:
        evidence_state = terminal_evidence_state(
            last_verification_status=last_verification_status,
            verification_after_last_write=verification_after_last_write,
            final_submit_present=final_submit_present,
            write_count=write_count,
            submit_count=submit_count,
            turn_limit_present=parse_bool(row.get("turn_limit_present")) or terminal_state == "turn_limit_unresolved",
            environment_error_present=parse_bool(row.get("environment_error_present"))
            or terminal_state == "tool_corrupted_terminal",
        )
    return {
        "task_id": task_id,
        "model": str(row.get("model") or row.get("model_key") or "").strip(),
        "harness": str(row.get("harness") or DEFAULT_HARNESS).strip(),
        "condition": normalized_condition,
        "condition_role": condition_role(normalized_condition),
        "attempt_id": attempt_id,
        "pass_index": pass_index,
        "resolved": resolved,
        "harness_outcome": harness_outcome,
        "num_turns": parse_int(row.get("num_turns")),
        "action_sequence": action_sequence,
        "early_exploration_intent_sequence": str(row.get("early_exploration_intent_sequence") or ""),
        "early_broad_inventory_count": parse_int(row.get("early_broad_inventory_count")),
        "early_targeted_lookup_count": parse_int(row.get("early_targeted_lookup_count")),
        "early_direct_read_count": parse_int(row.get("early_direct_read_count")),
        "early_test_probe_count": parse_int(row.get("early_test_probe_count")),
        "ask_count": parse_int(row.get("ask_count") or row.get("num_questions")),
        "irrelevant_ask_count": parse_int(row.get("irrelevant_ask_count")),
        "relevant_ask_count": parse_int(row.get("relevant_ask_count")),
        "ask_sequence": str(row.get("ask_sequence") or ""),
        "first_ask_turn": parse_int(row.get("first_ask_turn")),
        "first_write_turn": parse_int(row.get("first_write_turn")),
        "questions_before_first_edit": parse_int(row.get("questions_before_first_edit")),
        "write_count": write_count,
        "submit_count": submit_count,
        "test_event_count": parse_int(row.get("test_event_count")),
        "passed_test_count": parse_int(row.get("passed_test_count")),
        "failed_test_count": parse_int(row.get("failed_test_count")),
        "last_verification_command": str(row.get("last_verification_command") or ""),
        "last_verification_status": last_verification_status,
        "final_submit_present": final_submit_present,
        "verification_after_last_write": verification_after_last_write,
        "terminal_evidence_state": evidence_state,
        "ask_precision": row.get("ask_precision", ""),
        "blocker_recall": row.get("blocker_recall", ""),
        "ask_f1": row.get("ask_f1", ""),
        "terminal_state": terminal_state,
        "trajectory_path": str(row.get("trajectory_path") or ""),
    }


def _attempt_sort_key(row: dict[str, Any]) -> tuple[int, str]:
    return (parse_int(row.get("pass_index"), 1), str(row.get("attempt_id") or ""))


def task_pass_at_k(rows: Iterable[dict[str, Any]], k: int) -> dict[str, int]:
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        task_id = str(row.get("task_id") or "")
        if task_id:
            by_task[task_id].append(row)
    return {
        task_id: int(any(parse_bool(row.get("resolved")) for row in sorted(task_rows, key=_attempt_sort_key)[:k]))
        for task_id, task_rows in by_task.items()
    }


def mean_pass_at_k(rows: Iterable[dict[str, Any]], k: int) -> float:
    task_results = task_pass_at_k(rows, k)
    if not task_results:
        return math.nan
    return sum(task_results.values()) / len(task_results)


def paired_delta_label(full_info_pass: int | None, blocked_pass: int | None, ask_pass: int | None) -> str:
    pattern = (full_info_pass, blocked_pass, ask_pass)
    labels = {
        (1, 1, 1): "all_conditions_pass",
        (0, 0, 0): "all_conditions_fail",
        (1, 0, 0): "fullinfo_pass_blocked_fail_ask_fail",
        (1, 0, 1): "ask_recovers_blocked_failure",
        (1, 1, 0): "ask_regresses_despite_unblocked_success",
        (0, 1, 0): "blocked_pass_ask_fail_fullinfo_fail",
        (0, 1, 1): "blocked_and_ask_pass_fullinfo_fail",
        (0, 0, 1): "ask_recovers_without_fullinfo_baseline",
    }
    if pattern in labels:
        return labels[pattern]
    return "insufficient_condition_overlap"


def compare_task_conditions(
    *,
    task_id: str,
    model: str,
    harness: str,
    k: int,
    full_info_pass: int | None,
    blocked_pass: int | None,
    ask_pass: int | None,
) -> dict[str, Any]:
    max_unblocked = full_info_pass
    info_recovery_rate = math.nan
    if max_unblocked and ask_pass is not None:
        info_recovery_rate = ask_pass / max_unblocked
    return {
        "task_id": task_id,
        "model": model,
        "harness": harness,
        "k": k,
        "FullInfo@k": full_info_pass,
        "Blocked@k": blocked_pass,
        "AskPass@k": ask_pass,
        "Max_Unblocked@k": max_unblocked,
        "Clarification_Lift@k": math.nan
        if blocked_pass is None or ask_pass is None
        else ask_pass - blocked_pass,
        "Info_Recovery_Rate@k": info_recovery_rate,
        "Info_Loss@k": math.nan
        if max_unblocked is None or ask_pass is None
        else max_unblocked - ask_pass,
        "paired_delta_label": paired_delta_label(full_info_pass, blocked_pass, ask_pass),
    }


def build_per_task_comparisons(rows: list[dict[str, Any]], k: int) -> list[dict[str, Any]]:
    by_group: dict[tuple[str, str, str], dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        key = (str(row.get("task_id") or ""), str(row.get("model") or ""), str(row.get("harness") or DEFAULT_HARNESS))
        if key[0]:
            by_group[key][condition_role(str(row.get("condition_role") or row.get("condition") or ""))].append(row)

    comparisons: list[dict[str, Any]] = []
    for (task_id, model, harness), role_rows in sorted(by_group.items()):
        task_passes = {role: task_pass_at_k(role_rows.get(role, []), k).get(task_id) for role in ("full_info", "blocked", "ask")}
        comparisons.append(
            compare_task_conditions(
                task_id=task_id,
                model=model,
                harness=harness,
                k=k,
                full_info_pass=task_passes["full_info"],
                blocked_pass=task_passes["blocked"],
                ask_pass=task_passes["ask"],
            )
        )
    return comparisons


def _mean(values: Iterable[Any]) -> float:
    clean = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isnan(number):
            clean.append(number)
    if not clean:
        return math.nan
    return sum(clean) / len(clean)


def _has_metric(row: dict[str, Any], key: str) -> bool:
    value = row.get(key)
    if value is None or value == "":
        return False
    try:
        return not math.isnan(float(value))
    except (TypeError, ValueError):
        return True


def _intersected_comparison_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if _has_metric(row, "FullInfo@k") and _has_metric(row, "Blocked@k") and _has_metric(row, "AskPass@k")
    ]


def summarize_condition(rows: list[dict[str, Any]], k_values: Iterable[int]) -> list[dict[str, Any]]:
    by_group: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_group[(str(row.get("model") or ""), str(row.get("harness") or DEFAULT_HARNESS), str(row.get("condition") or ""))].append(row)

    summary: list[dict[str, Any]] = []
    for (model, harness, condition), group_rows in sorted(by_group.items()):
        task_ids = {str(row.get("task_id") or "") for row in group_rows if row.get("task_id")}
        terminal_counts = Counter(str(row.get("terminal_state") or "unknown") for row in group_rows)
        base = {
            "model": model,
            "harness": harness,
            "condition": condition,
            "condition_role": condition_role(condition),
            "num_tasks": len(task_ids),
            "num_runs": len(group_rows),
            "mean_ask_count": _mean(row.get("ask_count") for row in group_rows),
            "mean_questions_before_first_edit": _mean(row.get("questions_before_first_edit") for row in group_rows),
            "terminal_state_counts": json.dumps(dict(sorted(terminal_counts.items()))),
        }
        for k in k_values:
            row = dict(base)
            row["k"] = k
            row["pass@k"] = mean_pass_at_k(group_rows, k)
            summary.append(row)
    return summary


def aggregate_comparisons(comparisons: list[dict[str, Any]], k: int) -> list[dict[str, Any]]:
    by_group: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in comparisons:
        by_group[(str(row.get("model") or ""), str(row.get("harness") or DEFAULT_HARNESS))].append(row)
    out: list[dict[str, Any]] = []
    for (model, harness), rows in sorted(by_group.items()):
        intersected_rows = _intersected_comparison_rows(rows)
        labels = Counter(str(row.get("paired_delta_label") or "") for row in intersected_rows)
        out.append(
            {
                "model": model,
                "harness": harness,
                "k": k,
                "num_tasks": len(rows),
                "num_intersected_tasks": len(intersected_rows),
                "FullInfo@k": _mean(row.get("FullInfo@k") for row in intersected_rows),
                "Blocked@k": _mean(row.get("Blocked@k") for row in intersected_rows),
                "AskPass@k": _mean(row.get("AskPass@k") for row in intersected_rows),
                "Max_Unblocked@k": _mean(row.get("Max_Unblocked@k") for row in intersected_rows),
                "Clarification_Lift@k": _mean(row.get("Clarification_Lift@k") for row in intersected_rows),
                "Info_Recovery_Rate@k": _mean(row.get("Info_Recovery_Rate@k") for row in intersected_rows),
                "Info_Loss@k": _mean(row.get("Info_Loss@k") for row in intersected_rows),
                "paired_delta_label_counts": json.dumps(dict(sorted(labels.items()))),
            }
        )
    return out


def task_condition_overlap(comparisons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_group: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in comparisons:
        by_group[
            (
                str(row.get("model") or ""),
                str(row.get("harness") or DEFAULT_HARNESS),
                parse_int(row.get("k"), 1),
            )
        ].append(row)
    out: list[dict[str, Any]] = []
    for (model, harness, k), rows in sorted(by_group.items()):
        has_full = [_has_metric(row, "FullInfo@k") for row in rows]
        has_blocked = [_has_metric(row, "Blocked@k") for row in rows]
        has_ask = [_has_metric(row, "AskPass@k") for row in rows]
        out.append(
            {
                "model": model,
                "harness": harness,
                "k": k,
                "num_tasks": len(rows),
                "num_full_info": sum(has_full),
                "num_blocked": sum(has_blocked),
                "num_ask": sum(has_ask),
                "num_full_info_blocked": sum(full and blocked for full, blocked in zip(has_full, has_blocked)),
                "num_full_info_ask": sum(full and ask for full, ask in zip(has_full, has_ask)),
                "num_blocked_ask": sum(blocked and ask for blocked, ask in zip(has_blocked, has_ask)),
                "num_all_three": sum(full and blocked and ask for full, blocked, ask in zip(has_full, has_blocked, has_ask)),
            }
        )
    return out


def terminal_failure_mix_by_model(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_group: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for row in rows:
        by_group[(str(row.get("model") or ""), str(row.get("harness") or DEFAULT_HARNESS))][
            str(row.get("terminal_state") or "unknown")
        ] += 1
    out: list[dict[str, Any]] = []
    for (model, harness), counts in sorted(by_group.items()):
        total = sum(counts.values()) or 1
        for terminal_state, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
            out.append(
                {
                    "model": model,
                    "harness": harness,
                    "terminal_state": terminal_state,
                    "count": count,
                    "share": count / total,
                }
            )
    return out


def first_question_recovery_by_model(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_group: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_group[
            (
                str(row.get("model") or ""),
                str(row.get("harness") or DEFAULT_HARNESS),
                str(row.get("condition") or ""),
            )
        ].append(row)
    out: list[dict[str, Any]] = []
    for (model, harness, condition), group_rows in sorted(by_group.items()):
        asked = [row for row in group_rows if parse_int(row.get("first_ask_turn")) > 0]
        not_asked = [row for row in group_rows if parse_int(row.get("first_ask_turn")) <= 0]
        out.append(
            {
                "model": model,
                "harness": harness,
                "condition": condition,
                "num_runs": len(group_rows),
                "num_runs_with_ask": len(asked),
                "ask_rate": len(asked) / len(group_rows) if group_rows else math.nan,
                "mean_first_ask_turn": _mean(row.get("first_ask_turn") for row in asked),
                "mean_questions_before_first_edit": _mean(row.get("questions_before_first_edit") for row in group_rows),
                "resolved_rate_with_ask": _mean(int(parse_bool(row.get("resolved"))) for row in asked),
                "resolved_rate_without_ask": _mean(int(parse_bool(row.get("resolved"))) for row in not_asked),
            }
        )
    return out


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else math.nan


def bad_first_question_recovery_by_model(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_group: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        normalized = normalize_run_row(row)
        by_group[
            (
                str(normalized.get("model") or ""),
                str(normalized.get("harness") or DEFAULT_HARNESS),
                str(normalized.get("condition") or ""),
            )
        ].append(normalized)

    out: list[dict[str, Any]] = []
    for (model, harness, condition), group_rows in sorted(by_group.items()):
        asked = [row for row in group_rows if str(row.get("ask_sequence") or "")]
        bad_first = [row for row in asked if str(row.get("ask_sequence") or "").startswith("I")]
        good_first = [row for row in asked if str(row.get("ask_sequence") or "").startswith("R")]
        bad_first_with_second = [row for row in bad_first if len(str(row.get("ask_sequence") or "")) >= 2]
        bad_first_relevant_second = [row for row in bad_first if str(row.get("ask_sequence") or "")[1:2] == "R"]
        bad_first_irrelevant_second = [row for row in bad_first if str(row.get("ask_sequence") or "")[1:2] == "I"]
        out.append(
            {
                "model": model,
                "harness": harness,
                "condition": condition,
                "num_runs": len(group_rows),
                "num_runs_with_first_question": len(asked),
                "num_bad_first_question": len(bad_first),
                "bad_first_question_rate": _rate(len(bad_first), len(asked)),
                "num_good_first_question": len(good_first),
                "good_first_question_rate": _rate(len(good_first), len(asked)),
                "num_bad_first_with_second_question": len(bad_first_with_second),
                "second_question_after_bad_first_rate": _rate(len(bad_first_with_second), len(bad_first)),
                "num_relevant_second_question_after_bad_first": len(bad_first_relevant_second),
                "relevant_second_question_after_bad_first_rate": _rate(len(bad_first_relevant_second), len(bad_first_with_second)),
                "num_irrelevant_second_question_after_bad_first": len(bad_first_irrelevant_second),
                "irrelevant_second_question_after_bad_first_rate": _rate(
                    len(bad_first_irrelevant_second), len(bad_first_with_second)
                ),
                "resolved_rate_after_bad_first_question": _mean(int(parse_bool(row.get("resolved"))) for row in bad_first),
                "resolved_rate_after_bad_first_relevant_second": _mean(
                    int(parse_bool(row.get("resolved"))) for row in bad_first_relevant_second
                ),
                "resolved_rate_after_bad_first_irrelevant_second": _mean(
                    int(parse_bool(row.get("resolved"))) for row in bad_first_irrelevant_second
                ),
                "resolved_rate_after_good_first_question": _mean(int(parse_bool(row.get("resolved"))) for row in good_first),
            }
        )
    return out


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    fields = list(rows[0].keys())
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def load_run_csv(path: Path, *, condition: str | None = None) -> list[dict[str, Any]]:
    with path.open(newline="") as handle:
        return [normalize_run_row(row, condition=condition) for row in csv.DictReader(handle)]


def write_methodology_note(path: Path, *, k_values: Iterable[int]) -> None:
    path.write_text(
        "\n".join(
            [
                "# Trust Horizon v0 Methodology",
                "",
                "## Inputs",
                "",
                "This report consumes existing HIL-Bench trajectories, pass/fail labels, ask metrics, and run metadata.",
                "Terminal states may come from a cached terminal-label sweep joined by task/attempt/pass index; resolved=True traces are normalized to clean_pass.",
                "Action parsing, terminal-state normalization, overlap accounting, and cross-condition aggregation are produced by this tooling.",
                "",
                "## Metric Semantics",
                "",
                "Blocked@k, AskPass@k, Max_Unblocked@k, Info Recovery Rate@k, and Info Loss@k are cross-condition metrics.",
                "They are not per-trajectory labels.",
                "Headline aggregate metrics use tasks present in all three condition roles: full_info, blocked, and ask.",
                "task_condition_overlap.csv reports the union and intersection counts used to audit this gating.",
                "",
                f"Evaluated k values: {', '.join(str(k) for k in k_values)}.",
                "When Max_Unblocked@k is zero, Info Recovery Rate@k is undefined and reported as NaN.",
                "In v0, Max_Unblocked@k is equivalent to FullInfo@k because only one unblocked role is modeled.",
                "",
                "## Deferred",
                "",
                "The recovery funnel and behavior-label judge are deferred to v0.5.",
                "The current ask relevance signal is heuristic and intended only for lightweight first-question summaries.",
                "Condition inference prefers explicit metadata; s3_key inference is limited to exact path components.",
            ]
        )
        + "\n"
    )


def run_analysis(input_csvs: list[Path], output_dir: Path, *, k_values: list[int], conditions: list[str] | None = None) -> None:
    rows: list[dict[str, Any]] = []
    for idx, input_csv in enumerate(input_csvs):
        condition = conditions[idx] if conditions and idx < len(conditions) else None
        rows.extend(load_run_csv(input_csv, condition=condition))
    write_csv(output_dir / "per_run_analysis.csv", rows)
    write_csv(output_dir / "condition_summary.csv", summarize_condition(rows, k_values))
    terminal_mix = terminal_failure_mix_by_model(rows)
    first_question_rows = first_question_recovery_by_model(rows)
    bad_first_question_rows = bad_first_question_recovery_by_model(rows)
    write_csv(output_dir / "terminal_failure_mix_by_model.csv", terminal_mix)
    write_csv(output_dir / "first_question_recovery_by_model.csv", first_question_rows)
    write_csv(output_dir / "bad_first_question_recovery_by_model.csv", bad_first_question_rows)
    all_comparisons: list[dict[str, Any]] = []
    for idx, k in enumerate(k_values):
        comparisons = build_per_task_comparisons(rows, k)
        write_csv(output_dir / f"per_task_comparison_k{k}.csv", comparisons)
        tax_rows = aggregate_comparisons(comparisons, k)
        write_csv(output_dir / f"batch_comparison_summary_k{k}.csv", tax_rows)
        if idx == 0:
            write_csv(output_dir / "per_task_condition_comparison.csv", comparisons)
            write_csv(output_dir / "tax_decomposition_by_model.csv", tax_rows)
        all_comparisons.extend(comparisons)
    if all_comparisons:
        write_csv(output_dir / "per_task_comparison.csv", all_comparisons)
        write_csv(output_dir / "task_condition_overlap.csv", task_condition_overlap(all_comparisons))
    write_methodology_note(output_dir / "methodology_note.md", k_values=k_values)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Trust Horizon v0 analysis from per-run CSV inputs.")
    parser.add_argument("--input-csv", action="append", type=Path, required=True, help="Per-run CSV input. Repeat for multiple conditions.")
    parser.add_argument("--condition", action="append", help="Optional condition override for each --input-csv.")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--k", action="append", type=int)
    args = parser.parse_args(argv)
    run_analysis(args.input_csv, args.output_dir, k_values=args.k or [1], conditions=args.condition)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
