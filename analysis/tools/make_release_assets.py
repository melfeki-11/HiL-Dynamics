#!/usr/bin/env python3
"""Generate Trust Horizon release CSVs and lightweight SVG figures.

This intentionally avoids plotting dependencies. Raw run roots are supplied by
CLI flags, and the trajectory classifier helper is vendored beside this script
so the tool is inspectable without a separate local checkout.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import re
import shlex
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


TOOL_DIR = Path(__file__).resolve().parent
OUT_DIR = TOOL_DIR.parent
DATA_DIR = OUT_DIR / "data"
FIG_DIR = OUT_DIR / "figures"
MIN_EVAL_COVERAGE_FOR_FIGURES = 0.9
HARNESS_ROOT: Path | None = None
SKILL3_GLOB = "*_swe_skill3"
EXTRA_NATIVE_RUN_NAMES = {
    "adk_swe_full_info",
    "claude_swe_customtool",
    "claude_swe_full_info",
    "codex_swe_customtool2",
    "codex_swe_full_info",
    "opencode_swe_full_info",
}
NATIVE_RUN_SCAFFOLD_OVERRIDES = {
    "adk_swe_skill3": "adk",
    "claude_swe_skill3": "claude-code",
    "claude_swe_customtool": "claude-code-customtool",
    "claude_swe_full_info": "claude-code",
    "codex_swe_skill3": "codex",
    "codex_swe_customtool2": "codex-customtool",
    "codex_swe_full_info": "codex",
    "opencode_swe_skill3": "opencode",
    "opencode_swe_full_info": "opencode",
    "adk_swe_full_info": "adk",
}
SWE_AGENT_RAW_ROOT: Path | None = None
SWE_AGENT_ANALYSIS_ROOT: Path | None = None
SWE_AGENT_ANALYSIS_ONLY_MODEL_KEYS = {"gemini_3-1_pro_preview_customtools"}
HIL_BENCH_HARBOR_ROOTS: list[Path] = []
SCRUB_LOCAL_PATHS = False
PATH_FIELD_NAMES = {
    "pass_dir",
    "trajectory_path",
    "task_root",
    "registry_path",
    "root",
}
SECRET_TEXT_REPLACEMENTS = (
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "<redacted-api-key>"),
    (re.compile(r"\bghp_[A-Za-z0-9_]{16,}\b"), "<redacted-github-token>"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}\b"), "<redacted-github-token>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<redacted-aws-access-key>"),
    (re.compile(r"(?i)\b(authorization\s*:\s*bearer\s+)[A-Za-z0-9._~+/=-]{16,}"), r"\1<redacted>"),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{16,}"), r"\1<redacted>"),
    (
        re.compile(
            r"(?i)\b((?:OPENAI|ANTHROPIC|GOOGLE|GEMINI|FIREWORKS|LITELLM|AWS|AZURE|GITHUB|GH|HF|HUGGINGFACE)"
            r"[A-Z0-9_]*(?:API_)?(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS?)\s*[:=]\s*)"
            r"[\"']?[^\"'\s,;]+[\"']?"
        ),
        r"\1<redacted>",
    ),
)

from trust_horizon_core import (
    analyze_trajectory,
    ask_relevance,
    event_type,
    exploration_intent_class,
    terminal_evidence_state,
    test_outcome,
    validation_status,
)


MODEL_LABELS = {
    "gpt-5.5": "GPT-5.5",
    "gpt-5.4": "GPT-5.4",
    "gpt_5-5": "GPT-5.5",
    "gpt_5-4": "GPT-5.4",
    "anthropic/claude-opus-4-7": "Claude Opus 4.7",
    "claude-opus-4-7": "Claude Opus 4.7",
    "claude_opus_4-7": "Claude Opus 4.7",
    "gemini-3.1-pro-preview-customtools": "Gemini 3.1 Pro",
    "gemini/gemini-3.1-pro-preview-customtools": "Gemini 3.1 Pro",
    "google/gemini-3.1-pro-preview-customtools": "Gemini 3.1 Pro",
    "gemini_3-1_pro_preview_customtools": "Gemini 3.1 Pro",
    "fireworks_ai/glm-5p1": "GLM-5P1",
}

SCAFFOLD_LABELS = {
    "adk": "Native ADK",
    "claude": "Native Claude Code",
    "claude-code": "Native Claude Code",
    "claude-code-customtool": "Native Claude Code Tool",
    "codex": "Native Codex",
    "codex-customtool": "Native Codex Tool",
    "opencode": "Native OpenCode",
    "swe-agent": "SWE-agent",
}

TERMINAL_LABELS = {
    "visible_green_after_last_write": "local green / hidden red",
    "visible_red_at_end": "visible red at end",
    "weak_validation_only": "weak validation only",
    "unverified_patch_submitted": "untested final patch",
    "patch_no_submit": "patch made / no submit",
    "no_patch_or_no_submit": "no patch/no submit",
    "timeout_after_patch": "timeout after patch",
    "turn_limit_or_timeout": "timeout before patch",
    "tool_or_environment_error": "tool/env error",
    "unknown_terminal_evidence": "unknown",
}

TERMINAL_COLORS = {
    "visible_green_after_last_write": "#2f9e44",
    "visible_red_at_end": "#dc2626",
    "weak_validation_only": "#f59e0b",
    "unverified_patch_submitted": "#2563eb",
    "patch_no_submit": "#7c3aed",
    "no_patch_or_no_submit": "#475569",
    "timeout_after_patch": "#1971c2",
    "turn_limit_or_timeout": "#4dabf7",
    "tool_or_environment_error": "#c92a2a",
    "unknown_terminal_evidence": "#cbd5e1",
}

ACTION_PHENOTYPE_LABELS = {
    "ASK": "ASK",
    "READ": "READ",
    "WRITE": "WRITE",
    "TEST": "TEST",
    "EXECUTE": "EXECUTE",
    "GIT": "GIT",
    "SUBMIT": "SUBMIT",
    "THOUGHT_ONLY": "THOUGHT ONLY",
    "NO_ACTION": "NO TOOL",
    "OTHER": "OTHER",
    "IDLE_END": "IDLE/END",
}

ACTION_PHENOTYPE_COLORS = {
    "ASK": "#7c3aed",
    "READ": "#2563eb",
    "WRITE": "#dc2626",
    "TEST": "#16a34a",
    "EXECUTE": "#f59e0b",
    "GIT": "#64748b",
    "SUBMIT": "#0f766e",
    "THOUGHT_ONLY": "#94a3b8",
    "NO_ACTION": "#cbd5e1",
    "OTHER": "#a855f7",
    "IDLE_END": "#edf2f7",
}

ACTION_PHENOTYPE_LEGEND_ORDER = [
    "ASK",
    "READ",
    "WRITE",
    "TEST",
    "EXECUTE",
    "GIT",
    "SUBMIT",
    "THOUGHT_ONLY",
    "OTHER",
]
ACTION_PHENOTYPE_STACK_ORDER = ACTION_PHENOTYPE_LEGEND_ORDER
ACTION_PHENOTYPE_WITH_END_LEGEND_ORDER = ACTION_PHENOTYPE_LEGEND_ORDER + ["IDLE_END"]
ACTION_PHENOTYPE_WITH_END_STACK_ORDER = ACTION_PHENOTYPE_STACK_ORDER + ["IDLE_END"]
DEFAULT_PHENOTYPE_FAMILIES = ["GPT", "Claude", "Gemini"]

BLOCKER_LIFECYCLE_LABELS = {
    "resolved_run": "resolved run",
    "answer_received_acted_unresolved": "answer + follow-up, unresolved",
    "answer_received_no_followup": "answer, no concrete follow-up",
    "asked_no_relevant_answer": "asked, no matched answer",
    "discovered_not_asked": "discovered, not asked",
    "not_detected": "not detected",
}

BLOCKER_LIFECYCLE_COLORS = {
    "resolved_run": "#2f9e44",
    "answer_received_acted_unresolved": "#2563eb",
    "answer_received_no_followup": "#d9480f",
    "asked_no_relevant_answer": "#f59e0b",
    "discovered_not_asked": "#7c3aed",
    "not_detected": "#cbd5e1",
}

BLOCKER_LIFECYCLE_ORDER = [
    "resolved_run",
    "answer_received_acted_unresolved",
    "answer_received_no_followup",
    "asked_no_relevant_answer",
    "discovered_not_asked",
    "not_detected",
]

MODEL_FAMILY_ORDER = ["Claude", "GPT", "Gemini", "GLM", "Other"]

MODEL_FAMILY_SHAPES = {
    "Claude": "star",
    "GPT": "diamond",
    "Gemini": "triangle",
    "GLM": "square",
    "Other": "circle",
}

PERFORMANCE_COLOR_STOPS = (
    (0.0, "#c92a2a"),
    (0.5, "#f08c00"),
    (1.0, "#2f9e44"),
)

SHELL_LC_RE = re.compile(r"/bin/bash\s+-lc\s+(.+)$", re.S)
WRAPPED_WRITE_RE = re.compile(r"^\s*(Edit|Write|MultiEdit|NotebookEdit):", re.I)
WRAPPED_READ_RE = re.compile(r"^\s*(Read|Grep|Glob|WebFetch|WebSearch):", re.I)
GENERIC_EXECUTE_RE = re.compile(r"(^|\b)(python3?|node|ruby|perl|php|bash|sh)\b", re.I)
EXTRA_GIT_RE = re.compile(r"(^|\b)git\s+blame\b", re.I)
WEB_READ_RE = re.compile(r"(^|\b)(curl|wget)\b.+https?://", re.I | re.S)
THOUGHT_ASK_RE = re.compile(r"\b(ask|clarif|question|confirm|check with (?:the )?user)\b", re.I)
THOUGHT_WRITE_RE = re.compile(
    r"\b(edit|editing|write|writing|patch|patching|fix|fixing|implement|implementing|modify|modifying|change|changing|update|updating)\b",
    re.I,
)
THOUGHT_TEST_RE = re.compile(
    r"\b(run|runs|running|execute|executing|test|tests|testing|pytest|jest|compile|compiling|tsc|mypy|command|verify|verifying|verification)\b",
    re.I,
)
THOUGHT_READ_RE = re.compile(
    r"\b(read|reading|look|looking|inspect|inspecting|search|searching|find|finding|open|opening|examine|examining|check|checking)\b",
    re.I,
)
THOUGHT_FINAL_RE = re.compile(
    r"\b(done|implemented|fixed|summary|summarize|summarizing|final|complete|completed|finished)\b",
    re.I,
)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def safe_float(value: Any, default: float = math.nan) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "resolved", "pass", "passed", "agent_passed"}


def pct(value: float | None) -> str:
    if value is None or math.isnan(value):
        return "n/a"
    return f"{100 * value:.1f}%"


def is_path_field(key: str) -> bool:
    return key in PATH_FIELD_NAMES or key.endswith("_path") or key.endswith("_dir") or key.endswith("_root")


def scrub_local_path(value: Any) -> Any:
    if not SCRUB_LOCAL_PATHS or not isinstance(value, str) or not value.startswith("/"):
        return value
    parts = Path(value).parts
    if len(parts) <= 4:
        return "<local>/" + "/".join(part.strip("/") for part in parts if part != "/")
    return "<local>/" + "/".join(parts[-4:])


def redact_sensitive_text(value: str) -> str:
    for pattern, replacement in SECRET_TEXT_REPLACEMENTS:
        value = pattern.sub(replacement, value)
    return value


def scrub_row_for_output(row: dict[str, Any]) -> dict[str, Any]:
    return {key: scrub_local_path(value) if is_path_field(key) else value for key, value in row.items()}


def sanitize_for_output(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {child_key: sanitize_for_output(child_value, child_key) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [sanitize_for_output(item, key) for item in value]
    if is_path_field(key) or key == "full_info_dirs_found_outside_requested_roots":
        value = scrub_local_path(value)
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


def sanitize_row_for_output(row: dict[str, Any]) -> dict[str, Any]:
    return {key: sanitize_for_output(value, key) for key, value in row.items()}


def sanitize_rows_for_output(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [sanitize_row_for_output(row) for row in rows]


def scrub_for_output(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {child_key: scrub_for_output(child_value, child_key) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [scrub_for_output(item, key) for item in value]
    if is_path_field(key):
        value = scrub_local_path(value)
    if key == "full_info_dirs_found_outside_requested_roots":
        value = scrub_local_path(value)
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


def model_key(value: str) -> str:
    value = (value or "").strip()
    if value in MODEL_LABELS:
        return MODEL_LABELS[value]
    value = value.replace("openai/", "")
    return MODEL_LABELS.get(value, value or "unknown model")


def scaffold_label(value: str) -> str:
    return SCAFFOLD_LABELS.get((value or "").strip(), (value or "").strip() or "unknown scaffold")


def group_label(model: str, scaffold: str) -> str:
    return f"{model_key(model)} / {scaffold_label(scaffold)}"


def derived_ask_metrics(num_questions: int, blockers_resolved: int, blockers_total: int) -> tuple[float, float, float]:
    precision = blockers_resolved / num_questions if num_questions else 0.0
    recall = blockers_resolved / blockers_total if blockers_total else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def coerce_trajectory(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [event if isinstance(event, dict) else {} for event in data]
    if isinstance(data, dict):
        for key in ("trajectory", "events", "steps"):
            value = data.get(key)
            if isinstance(value, list):
                return [event if isinstance(event, dict) else {} for event in value]
    return []


def unwrap_shell_action(act: str) -> str:
    act = act or ""
    if act.startswith("Bash:"):
        try:
            payload = json.loads(act.split(":", 1)[1].strip())
            command = payload.get("command")
            if isinstance(command, str) and command.strip():
                return command
        except (IndexError, json.JSONDecodeError):
            return act
    match = SHELL_LC_RE.match(act)
    if match:
        command = match.group(1).strip()
        try:
            parts = shlex.split(command)
            if len(parts) == 1:
                return parts[0]
            if parts:
                return " ".join(parts)
        except ValueError:
            return command.strip("\"'")
    return act


def release_event_type(act: str) -> str:
    """Classify harness-specific action wrappers used in these release plots."""
    original_kind = event_type(act)
    if original_kind != "OTHER":
        return original_kind
    act = act or ""
    if WRAPPED_WRITE_RE.search(act):
        return "WRITE"
    if WRAPPED_READ_RE.search(act):
        return "READ"
    is_shell_wrapper = act.startswith("Bash:") or bool(SHELL_LC_RE.match(act))
    if not is_shell_wrapper:
        return original_kind
    command = unwrap_shell_action(act)
    command_kind = event_type(command)
    if command_kind != "OTHER":
        return command_kind
    if EXTRA_GIT_RE.search(command):
        return "GIT"
    if WEB_READ_RE.search(command):
        return "READ"
    if GENERIC_EXECUTE_RE.search(command):
        return "EXECUTE"
    return original_kind


def release_event_type_for_event(event: dict[str, Any]) -> str:
    act = str(event.get("act") or "")
    if not act.strip() and str(event.get("thought") or "").strip():
        return "THOUGHT_ONLY"
    return release_event_type(act)


def release_exploration_intent(act: str) -> str:
    return exploration_intent_class(unwrap_shell_action(act))


def reanalyze_release_trajectory(row: dict[str, Any], trajectory: list[dict[str, Any]]) -> dict[str, Any]:
    """Update trajectory-derived fields with local wrapper-aware action labels."""
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

    for turn, event in enumerate(trajectory, 1):
        event = event if isinstance(event, dict) else {}
        act = str(event.get("act") or "")
        obs = str(event.get("obs") or "")
        kind = release_event_type_for_event(event)
        event_types.append(kind)
        if not first_write_turn and kind != "WRITE":
            intent = release_exploration_intent(act)
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
            command = unwrap_shell_action(act)
            last_verification_command = " ".join(command.split())
            last_verification_status = validation_status(command, obs)
            last_verification_turn = turn
            outcome = test_outcome(obs)
            if outcome == "passed":
                passed_test_count += 1
            elif outcome == "failed":
                failed_test_count += 1

    final_submit_present = bool(submit_count and last_submit_turn == len(trajectory))
    verification_after_last_write = bool(last_verification_turn and (not last_write_turn or last_verification_turn > last_write_turn))
    evidence_state = terminal_evidence_state(
        last_verification_status=last_verification_status,
        verification_after_last_write=verification_after_last_write,
        final_submit_present=final_submit_present,
        write_count=write_count,
        submit_count=submit_count,
        turn_limit_present=safe_bool(row.get("turn_limit_present")),
        environment_error_present=safe_bool(row.get("environment_error_present")),
    )
    if evidence_state == "no_patch_or_no_submit" and safe_int(row.get("patch_bytes")) > 0:
        evidence_state = "patch_no_submit"

    questions_before_first_edit = 0
    if first_write_turn:
        questions_before_first_edit = sum(1 for idx, kind in enumerate(event_types, 1) if kind == "ASK" and idx < first_write_turn)
    row.update(
        {
            "action_sequence_raw_classifier": row.get("action_sequence", ""),
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
            "final_submit_present": final_submit_present,
            "verification_after_last_write": verification_after_last_write,
            "terminal_evidence_state_raw_classifier": row.get("terminal_evidence_state", ""),
            "terminal_evidence_state": evidence_state,
            "release_action_classifier": "wrapper_aware_v1",
        }
    )
    return row


def enrich_with_trajectory(base: dict[str, Any], trajectory_path: Path) -> dict[str, Any]:
    row = dict(base)
    row["trajectory_path"] = str(trajectory_path) if trajectory_path.exists() else ""
    if trajectory_path.exists():
        try:
            trajectory = coerce_trajectory(read_json(trajectory_path))
            if trajectory:
                analyzed = analyze_trajectory(trajectory, metadata=row, condition=str(base.get("condition") or ""))
                analyzed.update(row)
                row = reanalyze_release_trajectory(analyzed, trajectory)
                row["trajectory_analyzed"] = True
            else:
                row["trajectory_analyzed"] = False
                row["trajectory_error"] = "empty_or_unrecognized_trajectory"
        except Exception as exc:  # pragma: no cover - diagnostic artifact generation.
            row["trajectory_analyzed"] = False
            row["trajectory_error"] = f"{type(exc).__name__}: {exc}"
    else:
        row["trajectory_analyzed"] = False
        row["trajectory_error"] = "missing_trajectory"

    row["model_label"] = model_key(str(row.get("model") or ""))
    row["scaffold_label"] = scaffold_label(str(row.get("harness") or ""))
    row["group_label"] = group_label(str(row.get("model") or ""), str(row.get("harness") or ""))
    row["is_native"] = str(row.get("harness") or "") != "swe-agent"
    return row


def row_from_native_pass_level(pass_row: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    pass_dir = Path(str(pass_row.get("pass_dir") or ""))
    if not pass_dir.exists():
        pass_dir = run_dir / str(pass_row.get("uid") or "") / str(pass_row.get("mode") or "ask_human") / f"pass_{pass_row.get('pass_index')}"
    num_questions = safe_int(pass_row.get("num_questions"))
    blockers_resolved = safe_int(pass_row.get("num_blockers_resolved"))
    blockers_total = safe_int(pass_row.get("num_blockers_total"))
    ask_precision, blocker_recall, ask_f1 = derived_ask_metrics(num_questions, blockers_resolved, blockers_total)
    status = str(pass_row.get("status") or "")
    eval_known = status.strip().lower() not in {"infra_error", "eval_error", "missing_eval"}
    scaffold = NATIVE_RUN_SCAFFOLD_OVERRIDES.get(
        run_dir.name,
        str(pass_row.get("agent") or run_dir.name.replace("_swe_skill3", "")),
    )
    base = {
        "source": "native_release",
        "run_id": run_dir.name,
        "task_id": str(pass_row.get("uid") or ""),
        "attempt_id": str(pass_row.get("uid") or ""),
        "condition": str(pass_row.get("mode") or "ask_human"),
        "harness": scaffold,
        "model": str(pass_row.get("model") or ""),
        "pass_index": safe_int(pass_row.get("pass_index"), 1),
        "resolved": safe_bool(pass_row.get("resolved")) if eval_known else False,
        "eval_known": eval_known,
        "status": status,
        "num_questions": num_questions,
        "num_blockers_resolved": blockers_resolved,
        "num_blockers_total": blockers_total,
        "ask_precision": ask_precision,
        "blocker_recall": blocker_recall,
        "ask_f1": ask_f1,
        "patch_bytes": safe_int(pass_row.get("patch_bytes")),
        "pass_dir": str(pass_dir),
    }
    return enrich_with_trajectory(base, pass_dir / "trajectory.json")


def row_from_native_pass_dir(pass_dir: Path, run_dir: Path) -> dict[str, Any]:
    attempt = read_json(pass_dir / "attempt.json") if (pass_dir / "attempt.json").exists() else {}
    stats = read_json(pass_dir / "stats.json") if (pass_dir / "stats.json").exists() else {}
    result = read_json(pass_dir / "result.json") if (pass_dir / "result.json").exists() else {}
    eval_result = read_json(pass_dir / "eval_result.json") if (pass_dir / "eval_result.json").exists() else {}
    num_questions = safe_int(stats.get("num_questions"))
    blockers_resolved = safe_int(stats.get("num_blockers_resolved"))
    blockers_total = safe_int(stats.get("num_blockers_total"))
    ask_precision, blocker_recall, ask_f1 = derived_ask_metrics(num_questions, blockers_resolved, blockers_total)
    eval_known = bool(eval_result)
    resolved = safe_bool(eval_result.get("resolved")) if eval_known else False
    uid = str(attempt.get("uid") or pass_dir.parents[1].name)
    scaffold = NATIVE_RUN_SCAFFOLD_OVERRIDES.get(
        run_dir.name,
        str(attempt.get("harness") or run_dir.name.replace("_swe_skill3", "")),
    )
    base = {
        "source": "native_release",
        "run_id": run_dir.name,
        "task_id": uid,
        "attempt_id": uid,
        "condition": str(attempt.get("mode") or "ask_human"),
        "harness": scaffold,
        "model": str(attempt.get("model") or ""),
        "pass_index": safe_int(attempt.get("pass_index") or pass_dir.name.replace("pass_", ""), 1),
        "resolved": resolved,
        "eval_known": eval_known,
        "status": str(eval_result.get("eval_status") or result.get("stop_reason") or ""),
        "num_questions": num_questions,
        "num_blockers_resolved": blockers_resolved,
        "num_blockers_total": blockers_total,
        "ask_precision": ask_precision,
        "blocker_recall": blocker_recall,
        "ask_f1": ask_f1,
        "patch_bytes": safe_int(result.get("patch_bytes")),
        "pass_dir": str(pass_dir),
        "timeout": safe_bool(result.get("timeout")),
        "sdk_error": str(result.get("sdk_error") or ""),
    }
    return enrich_with_trajectory(base, pass_dir / "trajectory.json")


def ingest_native_skill3() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    verification: list[dict[str, Any]] = []
    if HARNESS_ROOT is None:
        verification.append(
            {
                "root": "",
                "exists": False,
                "pass_dirs": 0,
                "ask_human_pass_dirs": 0,
                "full_info_pass_dirs": 0,
                "has_summary_json": False,
                "has_pass_level_json": False,
                "note": "native runs root not configured",
            }
        )
        return rows, verification
    if not HARNESS_ROOT.exists():
        verification.append(
            {
                "root": str(HARNESS_ROOT),
                "exists": False,
                "pass_dirs": 0,
                "ask_human_pass_dirs": 0,
                "full_info_pass_dirs": 0,
                "has_summary_json": False,
                "has_pass_level_json": False,
                "note": "native runs root missing",
            }
        )
        return rows, verification
    run_dirs = {path for path in HARNESS_ROOT.glob(SKILL3_GLOB)}
    run_dirs.update(HARNESS_ROOT / name for name in EXTRA_NATIVE_RUN_NAMES)
    for run_dir in sorted(path for path in run_dirs if path.exists()):
        pass_level_path = run_dir / "metrics" / "pass_level.json"
        summary_path = run_dir / "metrics" / "summary.json"
        ask_pass_dirs = sorted(run_dir.glob("*/ask_human/pass_*"))
        full_info_pass_dirs = sorted(run_dir.glob("*/full_info/pass_*"))
        pass_dirs = sorted(ask_pass_dirs + full_info_pass_dirs)
        verification.append(
            {
                "root": str(run_dir),
                "exists": run_dir.exists(),
                "pass_dirs": len(pass_dirs),
                "ask_human_pass_dirs": len(ask_pass_dirs),
                "full_info_pass_dirs": len(full_info_pass_dirs),
                "has_summary_json": summary_path.exists(),
                "has_pass_level_json": pass_level_path.exists(),
            }
        )
        if pass_level_path.exists():
            for pass_row in read_json(pass_level_path):
                rows.append(row_from_native_pass_level(pass_row, run_dir))
        else:
            for pass_dir in pass_dirs:
                rows.append(row_from_native_pass_dir(pass_dir, run_dir))
    return rows, verification


def row_from_swe_agent_metrics(metrics_path: Path) -> dict[str, Any]:
    metrics = read_json(metrics_path)
    pass_dir = metrics_path.parent
    num_questions = safe_int(metrics.get("num_questions"))
    blockers_resolved = safe_int(metrics.get("num_blockers_resolved"))
    blockers_total = safe_int(metrics.get("num_blockers"))
    ask_precision, blocker_recall, ask_f1 = derived_ask_metrics(num_questions, blockers_resolved, blockers_total)
    base = {
        "source": "swe_agent_raw",
        "run_id": pass_dir.parents[1].name,
        "task_id": str(metrics.get("attempt_id") or pass_dir.parent.name),
        "attempt_id": str(metrics.get("attempt_id") or pass_dir.parent.name),
        "condition": str(metrics.get("mode_name") or "ask_human"),
        "harness": "swe-agent",
        "model": str(metrics.get("model_name") or pass_dir.parents[1].name),
        "pass_index": safe_int(metrics.get("pass_num") or pass_dir.name.replace("pass_", ""), 1),
        "resolved": safe_bool(metrics.get("resolved")),
        "eval_known": True,
        "status": str(metrics.get("status") or ""),
        "num_questions": num_questions,
        "num_blockers_resolved": blockers_resolved,
        "num_blockers_total": blockers_total,
        "ask_precision": ask_precision,
        "blocker_recall": blocker_recall,
        "ask_f1": ask_f1,
        "patch_bytes": len(str(metrics.get("prediction") or "")),
        "pass_dir": str(pass_dir),
    }
    return enrich_with_trajectory(base, pass_dir / "trajectory")


def ingest_swe_agent_raw() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    verification: list[dict[str, Any]] = []
    if SWE_AGENT_RAW_ROOT is None:
        verification.append(
            {
                "root": "",
                "exists": False,
                "metrics_files": 0,
                "trajectory_files": 0,
                "has_dataset_metrics_json": False,
                "note": "SWE-agent raw root not configured",
            }
        )
        return rows, verification
    if not SWE_AGENT_RAW_ROOT.exists():
        verification.append(
            {
                "root": str(SWE_AGENT_RAW_ROOT),
                "exists": False,
                "metrics_files": 0,
                "trajectory_files": 0,
                "has_dataset_metrics_json": False,
                "note": "SWE-agent raw root missing",
            }
        )
        return rows, verification
    for model_dir in sorted(path for path in SWE_AGENT_RAW_ROOT.iterdir() if path.is_dir()):
        metrics_paths = sorted(model_dir.glob("*/pass_*/metrics"))
        trajectory_paths = sorted(model_dir.glob("*/pass_*/trajectory"))
        verification.append(
            {
                "root": str(model_dir),
                "exists": model_dir.exists(),
                "metrics_files": len(metrics_paths),
                "trajectory_files": len(trajectory_paths),
                "has_dataset_metrics_json": (model_dir / "dataset_metrics.json").exists(),
            }
        )
        for metrics_path in metrics_paths:
            rows.append(row_from_swe_agent_metrics(metrics_path))
    return rows, verification


def ingest_swe_agent_analysis_only() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Ingest selected SWE-agent rows available only as derived analysis CSVs.

    These rows are useful for headline pass/ask/strategy comparisons but do not
    have raw terminal evidence, so terminal failure mix excludes this source.
    """
    if SWE_AGENT_ANALYSIS_ROOT is None:
        verification = [
            {
                "root": "",
                "first_ask_relevance_csv": False,
                "trajectory_summary_csv": False,
                "turn_actions_csv": False,
                "model_keys_requested": sorted(SWE_AGENT_ANALYSIS_ONLY_MODEL_KEYS),
                "rows_ingested": 0,
                "note": "SWE-agent analysis root not configured",
            }
        ]
        return [], verification

    first_ask_path = SWE_AGENT_ANALYSIS_ROOT / "failure_mode_investigation" / "first_ask_relevance_analysis.csv"
    trajectory_path = SWE_AGENT_ANALYSIS_ROOT / "trajectory_summary_model_families.csv"
    turn_actions_path = SWE_AGENT_ANALYSIS_ROOT / "turn_actions_model_families.csv"
    verification = [
        {
            "root": str(SWE_AGENT_ANALYSIS_ROOT),
            "first_ask_relevance_csv": first_ask_path.exists(),
            "trajectory_summary_csv": trajectory_path.exists(),
            "turn_actions_csv": turn_actions_path.exists(),
            "model_keys_requested": sorted(SWE_AGENT_ANALYSIS_ONLY_MODEL_KEYS),
            "rows_ingested": 0,
        }
    ]
    if not (first_ask_path.exists() and trajectory_path.exists() and turn_actions_path.exists()):
        return [], verification

    trajectory_by_key: dict[tuple[str, str, int], dict[str, str]] = {}
    for row in read_csv_rows(trajectory_path):
        model_id = str(row.get("model_key") or "")
        if model_id in SWE_AGENT_ANALYSIS_ONLY_MODEL_KEYS:
            trajectory_by_key[(model_id, str(row.get("attempt_id") or ""), safe_int(row.get("pass_num"), 1))] = row

    actions_by_key: dict[tuple[str, str, int], list[tuple[int, str]]] = defaultdict(list)
    for row in read_csv_rows(turn_actions_path):
        model_id = str(row.get("model_key") or "")
        if model_id in SWE_AGENT_ANALYSIS_ONLY_MODEL_KEYS:
            key = (model_id, str(row.get("attempt_id") or ""), safe_int(row.get("pass_num"), 1))
            actions_by_key[key].append((safe_int(row.get("turn_index") or row.get("turn")), str(row.get("action_type") or row.get("action") or "")))

    rows: list[dict[str, Any]] = []
    for ask_row in read_csv_rows(first_ask_path):
        model_id = str(ask_row.get("model_key") or "")
        if model_id not in SWE_AGENT_ANALYSIS_ONLY_MODEL_KEYS:
            continue
        pass_index = safe_int(ask_row.get("pass_num"), 1)
        key = (model_id, str(ask_row.get("attempt_id") or ""), pass_index)
        trajectory_row = trajectory_by_key.get(key, {})
        sequence = [
            "THOUGHT_ONLY" if action == "NO_ACTION" else action
            for _, action in sorted(actions_by_key.get(key, []))
            if action
        ]
        first_write_turn = next((idx + 1 for idx, action in enumerate(sequence) if action == "WRITE"), 0)
        first_test_turn = next((idx + 1 for idx, action in enumerate(sequence) if action == "TEST"), 0)
        num_questions = safe_int(ask_row.get("questions") or ask_row.get("ask_count"))
        blockers_resolved = safe_int(ask_row.get("blockers_resolved"))
        blockers_total = safe_int(ask_row.get("blockers"))
        ask_precision, blocker_recall, ask_f1 = derived_ask_metrics(num_questions, blockers_resolved, blockers_total)
        write_count = safe_int(trajectory_row.get("WRITE"))
        submit_count = safe_int(trajectory_row.get("SUBMIT"))
        resolved = safe_bool(ask_row.get("resolved"))
        row: dict[str, Any] = {
            "source": "swe_agent_analysis_csv",
            "run_id": "figure10_model_families",
            "task_id": key[1],
            "attempt_id": key[1],
            "condition": "ask_human",
            "harness": "swe-agent",
            "model": model_id,
            "pass_index": pass_index,
            "resolved": resolved,
            "eval_known": True,
            "status": "resolved" if resolved else "unresolved",
            "num_questions": num_questions,
            "num_blockers_resolved": blockers_resolved,
            "num_blockers_total": blockers_total,
            "ask_precision": ask_precision,
            "blocker_recall": blocker_recall,
            "ask_f1": ask_f1,
            "patch_bytes": 0,
            "pass_dir": "",
            "trajectory_path": str(turn_actions_path),
            "trajectory_analyzed": False,
            "trajectory_error": "analysis_csv_only",
            "num_turns": safe_int(trajectory_row.get("num_turns") or ask_row.get("num_turns")),
            "action_sequence": ",".join(sequence),
            "ask_count": safe_int(trajectory_row.get("ASK") or ask_row.get("ask_count")),
            "irrelevant_ask_count": 1 if safe_bool(ask_row.get("first_ask_irrelevant")) else 0,
            "relevant_ask_count": max(0, num_questions - (1 if safe_bool(ask_row.get("first_ask_irrelevant")) else 0)),
            "first_ask_turn": safe_int(ask_row.get("first_ask_turn")),
            "first_write_turn": first_write_turn,
            "first_test_turn": first_test_turn,
            "questions_before_first_edit": sum(1 for action in sequence[: max(first_write_turn - 1, 0)] if action == "ASK") if first_write_turn else safe_int(trajectory_row.get("ASK")),
            "write_count": write_count,
            "submit_count": submit_count,
            "test_event_count": safe_int(trajectory_row.get("TEST")),
            "final_submit_present": submit_count > 0,
            "terminal_evidence_state": "analysis_csv_only",
            "terminal_state": "analysis_csv_only",
            "model_label": model_key(model_id),
            "scaffold_label": scaffold_label("swe-agent"),
            "group_label": group_label(model_id, "swe-agent"),
            "is_native": False,
        }
        rows.append(row)

    verification[0]["rows_ingested"] = len(rows)
    return rows, verification


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    rows = sanitize_rows_for_output(rows)
    fields: list[str] = []
    seen = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fields.append(key)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def pass_at_k(rows: list[dict[str, Any]], k: int) -> tuple[float, int, int]:
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if not safe_bool(row.get("eval_known")):
            continue
        task_id = str(row.get("task_id") or "")
        if task_id:
            by_task[task_id].append(row)
    denom = 0
    solved = 0
    for task_rows in by_task.values():
        sorted_rows = sorted(task_rows, key=lambda row: safe_int(row.get("pass_index"), 1))
        first_k = [row for row in sorted_rows if safe_int(row.get("pass_index"), 1) <= k]
        if len(first_k) < k:
            continue
        denom += 1
        solved += int(any(safe_bool(row.get("resolved")) for row in first_k))
    return (solved / denom if denom else math.nan, solved, denom)


def summarize_groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_group: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_group[
            (
                str(row.get("source") or ""),
                str(row.get("harness") or ""),
                str(row.get("model") or ""),
                str(row.get("condition") or ""),
            )
        ].append(row)

    summaries: list[dict[str, Any]] = []
    for (source, harness, model, condition), group_rows in sorted(by_group.items()):
        eval_rows = [row for row in group_rows if safe_bool(row.get("eval_known"))]
        q = sum(safe_int(row.get("num_questions")) for row in group_rows)
        br = sum(safe_int(row.get("num_blockers_resolved")) for row in group_rows)
        bt = sum(safe_int(row.get("num_blockers_total")) for row in group_rows)
        precision, recall, f1 = derived_ask_metrics(q, br, bt)
        p1, p1_solved, p1_n = pass_at_k(group_rows, 1)
        p2, p2_solved, p2_n = pass_at_k(group_rows, 2)
        p3, p3_solved, p3_n = pass_at_k(group_rows, 3)
        task_ids = {str(row.get("task_id") or "") for row in group_rows if row.get("task_id")}
        summaries.append(
            {
                "source": source,
                "harness": harness,
                "scaffold_label": scaffold_label(harness),
                "model": model,
                "model_label": model_key(model),
                "group_label": group_label(model, harness),
                "condition": condition,
                "num_tasks_seen": len(task_ids),
                "num_pass_rows": len(group_rows),
                "num_eval_known_rows": len(eval_rows),
                "eval_coverage": len(eval_rows) / len(group_rows) if group_rows else math.nan,
                "num_trajectory_analyzed_rows": sum(safe_bool(row.get("trajectory_analyzed")) for row in group_rows),
                "pass_at_1": p1,
                "pass_at_1_solved": p1_solved,
                "pass_at_1_n": p1_n,
                "pass_at_2": p2,
                "pass_at_2_solved": p2_solved,
                "pass_at_2_n": p2_n,
                "pass_at_3": p3,
                "pass_at_3_solved": p3_solved,
                "pass_at_3_n": p3_n,
                "ask_precision": precision,
                "ask_recall": recall,
                "ask_f1": f1,
                "avg_questions_per_pass": q / len(group_rows) if group_rows else math.nan,
                "total_questions": q,
                "total_blockers_resolved": br,
                "total_blockers_total": bt,
            }
        )
    return summaries


def full_info_gap_rows(summaries: list[dict[str, Any]], pass_rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    ask_rows = [
        row
        for row in summaries
        if row.get("condition") == "ask_human"
        and str(row.get("scaffold_label") or "").startswith("Native ")
        and "Tool" not in str(row.get("scaffold_label") or "")
        and plot_ready(row)
    ]
    full_rows = [
        row
        for row in summaries
        if row.get("condition") == "full_info"
        and str(row.get("scaffold_label") or "").startswith("Native ")
        and plot_ready(row)
    ]
    full_by_key = {
        (str(row.get("model_label") or ""), str(row.get("scaffold_label") or "")): row
        for row in full_rows
    }
    out: list[dict[str, Any]] = []
    for ask in ask_rows:
        key = (str(ask.get("model_label") or ""), str(ask.get("scaffold_label") or ""))
        full = full_by_key.get(key)
        if not full:
            continue
        comparison_scope = "all_available"
        num_intersected_tasks = ""
        ask_rows_for_metrics: list[dict[str, Any]] = []
        full_rows_for_metrics: list[dict[str, Any]] = []
        if pass_rows is not None:
            ask_group_rows = [
                row
                for row in pass_rows
                if row.get("condition") == "ask_human"
                and row.get("group_label") == ask.get("group_label")
                and row.get("task_id")
            ]
            full_group_rows = [
                row
                for row in pass_rows
                if row.get("condition") == "full_info"
                and row.get("group_label") == full.get("group_label")
                and row.get("task_id")
            ]
            ask_task_ids = {str(row.get("task_id") or "") for row in ask_group_rows}
            full_task_ids = {str(row.get("task_id") or "") for row in full_group_rows}
            intersected_task_ids = ask_task_ids & full_task_ids
            if intersected_task_ids:
                comparison_scope = "intersected_tasks"
                num_intersected_tasks = len(intersected_task_ids)
                ask_rows_for_metrics = [row for row in ask_group_rows if str(row.get("task_id") or "") in intersected_task_ids]
                full_rows_for_metrics = [row for row in full_group_rows if str(row.get("task_id") or "") in intersected_task_ids]

        if ask_rows_for_metrics and full_rows_for_metrics:
            ask_pass, ask_solved, ask_n = pass_at_k(ask_rows_for_metrics, 3)
            full_pass, full_solved, full_n = pass_at_k(full_rows_for_metrics, 3)
            ask_pass_rows = len(ask_rows_for_metrics)
            full_pass_rows = len(full_rows_for_metrics)
            ask_eval_coverage = sum(safe_bool(row.get("eval_known")) for row in ask_rows_for_metrics) / ask_pass_rows if ask_pass_rows else math.nan
            full_eval_coverage = sum(safe_bool(row.get("eval_known")) for row in full_rows_for_metrics) / full_pass_rows if full_pass_rows else math.nan
        else:
            ask_pass = safe_float(ask.get("pass_at_3"), math.nan)
            full_pass = safe_float(full.get("pass_at_3"), math.nan)
            ask_solved = ask.get("pass_at_3_solved", "")
            full_solved = full.get("pass_at_3_solved", "")
            ask_n = ask.get("pass_at_3_n", "")
            full_n = full.get("pass_at_3_n", "")
            ask_pass_rows = ask.get("num_pass_rows", "")
            full_pass_rows = full.get("num_pass_rows", "")
            ask_eval_coverage = ask.get("eval_coverage", "")
            full_eval_coverage = full.get("eval_coverage", "")
        out.append(
            {
                "model_label": ask.get("model_label", ""),
                "scaffold_label": ask.get("scaffold_label", ""),
                "group_label": ask.get("group_label", ""),
                "comparison_scope": comparison_scope,
                "num_intersected_tasks": num_intersected_tasks,
                "ask_human_pass_at_3": ask_pass,
                "full_info_pass_at_3": full_pass,
                "full_info_minus_ask_human_pp": (full_pass - ask_pass) * 100 if not math.isnan(ask_pass) and not math.isnan(full_pass) else math.nan,
                "ask_human_pass_at_3_solved": ask_solved,
                "full_info_pass_at_3_solved": full_solved,
                "ask_human_pass_at_3_n": ask_n,
                "full_info_pass_at_3_n": full_n,
                "ask_human_rows": ask_pass_rows,
                "full_info_rows": full_pass_rows,
                "ask_human_eval_coverage": ask_eval_coverage,
                "full_info_eval_coverage": full_eval_coverage,
            }
        )
    return sorted(out, key=lambda row: (-safe_float(row.get("full_info_pass_at_3"), -1), str(row.get("group_label") or "")))


def plot_ready(row: dict[str, Any]) -> bool:
    return safe_float(row.get("eval_coverage"), 0.0) >= MIN_EVAL_COVERAGE_FOR_FIGURES and not math.isnan(safe_float(row.get("pass_at_3")))


def terminal_evidence_bucket(row: dict[str, Any]) -> str:
    state = str(row.get("terminal_evidence_state") or "unknown_terminal_evidence")
    has_patch = safe_int(row.get("patch_bytes")) > 0 or safe_int(row.get("write_count")) > 0
    if state == "no_patch_or_no_submit" and has_patch:
        return "patch_no_submit"
    if state == "turn_limit_or_timeout" and has_patch:
        return "timeout_after_patch"
    return state


def terminal_mix(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if str(row.get("source") or "") == "swe_agent_analysis_csv":
            continue
        if row.get("condition") == "ask_human" and safe_bool(row.get("eval_known")) and not safe_bool(row.get("resolved")):
            groups[str(row.get("group_label") or "")].append(row)
    out: list[dict[str, Any]] = []
    for label, group_rows in sorted(groups.items()):
        counts = Counter(terminal_evidence_bucket(row) for row in group_rows)
        total = sum(counts.values())
        for state, count in sorted(counts.items()):
            out.append(
                {
                    "group_label": label,
                    "terminal_evidence_state": state,
                    "terminal_label": TERMINAL_LABELS.get(state, state),
                    "count": count,
                    "share": count / total if total else math.nan,
                    "unresolved_rows": total,
                }
            )
    return out


def action_audit_bucket(act: str) -> str:
    act = act or ""
    if not act.strip():
        return "empty act / thought-only turn"
    if act.startswith("/bin/bash -lc"):
        return "codex /bin/bash -lc wrapper"
    if act.startswith("Bash:"):
        return "claude Bash wrapper"
    if ":" in act[:40]:
        return f"{act.split(':', 1)[0]} wrapper"
    return "raw command/prose"


def thought_only_intent_bucket(thought: str) -> str:
    if THOUGHT_ASK_RE.search(thought):
        return "clarification intent"
    if THOUGHT_WRITE_RE.search(thought):
        return "edit/patch intent"
    if THOUGHT_TEST_RE.search(thought):
        return "run/test intent"
    if THOUGHT_READ_RE.search(thought):
        return "inspect/read intent"
    if THOUGHT_FINAL_RE.search(thought):
        return "final/summary text"
    return "planning/reflection"


def thought_only_no_tool_audit(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[tuple[str, str], dict[str, Any]] = {}
    totals = Counter()
    for row in rows:
        path = Path(str(row.get("trajectory_path") or ""))
        if path.name != "trajectory.json" or not path.exists() or row.get("condition") != "ask_human":
            continue
        try:
            trajectory = coerce_trajectory(read_json(path))
        except Exception:
            continue
        for event in trajectory:
            event = event if isinstance(event, dict) else {}
            act = str(event.get("act") or "")
            thought = str(event.get("thought") or "")
            if act.strip() or not thought.strip():
                continue
            label = str(row.get("group_label") or "")
            bucket = thought_only_intent_bucket(thought)
            totals[label] += 1
            item = counts.setdefault(
                (label, bucket),
                {
                    "group_label": label,
                    "thought_only_intent": bucket,
                    "count": 0,
                    "example_thought": "",
                },
            )
            item["count"] += 1
            if not item["example_thought"]:
                item["example_thought"] = " ".join(thought.split())[:500]
    out = list(counts.values())
    for item in out:
        total = totals[item["group_label"]]
        item["share_of_thought_only_turns"] = item["count"] / total if total else math.nan
    return sorted(out, key=lambda item: (item["group_label"], -safe_int(item["count"]), item["thought_only_intent"]))


def native_other_no_action_audit(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    targets = {"GPT-5.5 / Native Codex", "Claude Opus 4.7 / Native Claude Code"}
    counts: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    totals = Counter()
    for row in rows:
        label = str(row.get("group_label") or "")
        if row.get("condition") != "ask_human":
            continue
        if label not in targets:
            continue
        path = Path(str(row.get("trajectory_path") or ""))
        if not path.exists():
            continue
        try:
            trajectory = coerce_trajectory(read_json(path))
        except Exception:
            continue
        for event in trajectory:
            event = event if isinstance(event, dict) else {}
            act = str(event.get("act") or "")
            old_kind = event_type(act)
            new_kind = release_event_type_for_event(event)
            if old_kind not in {"OTHER", "NO_ACTION"} and new_kind not in {
                "OTHER",
                "NO_ACTION",
                "THOUGHT_ONLY",
            }:
                continue
            bucket = action_audit_bucket(act)
            key = (label, old_kind, new_kind, bucket)
            totals[label] += 1
            item = counts.setdefault(
                key,
                {
                    "group_label": label,
                    "raw_classifier_action": old_kind,
                    "release_classifier_action": new_kind,
                    "raw_bucket": bucket,
                    "count": 0,
                    "example_action": "",
                    "example_unwrapped_action": "",
                },
            )
            item["count"] += 1
            if not item["example_action"]:
                item["example_action"] = " ".join(act.split())[:500]
                item["example_unwrapped_action"] = " ".join(unwrap_shell_action(act).split())[:500]
    out = list(counts.values())
    for item in out:
        total = totals[item["group_label"]]
        item["share_of_audited_events"] = item["count"] / total if total else math.nan
    return sorted(out, key=lambda item: (item["group_label"], item["raw_classifier_action"], item["release_classifier_action"], -safe_int(item["count"])))


def classify_strategy(row: dict[str, Any]) -> str:
    ask_count = safe_int(row.get("ask_count") or row.get("num_questions"))
    if ask_count <= 0:
        return "no ask"
    sequence = [part for part in str(row.get("action_sequence") or "").split(",") if part]
    first_ask = safe_int(row.get("first_ask_turn"))
    first_write = safe_int(row.get("first_write_turn"))
    if not first_ask and "ASK" not in sequence:
        return "ask logged, no ask action"
    if first_ask:
        prior = set(sequence[: max(first_ask - 1, 0)])
        explored = bool(prior & {"READ", "GIT", "TEST", "EXECUTE"})
        if not explored:
            return "upfront ask before read"
        if not first_write or first_ask < first_write:
            return "explored then asked before write"
    if first_write and (not first_ask or first_write < first_ask):
        return "wrote before first ask"
    return "other mixed strategy"


def strategy_buckets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bucket_rows: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("condition") == "ask_human" and safe_bool(row.get("eval_known")):
            bucket_rows[(str(row.get("group_label") or ""), classify_strategy(row))].append(row)
    out: list[dict[str, Any]] = []
    for (label, bucket), group_rows in sorted(bucket_rows.items()):
        total = len(group_rows)
        solved = sum(safe_bool(row.get("resolved")) for row in group_rows)
        out.append(
            {
                "group_label": label,
                "strategy_bucket": bucket,
                "count": total,
                "share_within_group": math.nan,
                "pass_rate_per_pass_row": solved / total if total else math.nan,
            }
        )
    totals = Counter(row["group_label"] for row in out for _ in range(row["count"]))
    for row in out:
        row["share_within_group"] = row["count"] / totals[row["group_label"]] if totals[row["group_label"]] else math.nan
    return out


def canonical_action_for_phenotype(action: str) -> str:
    action = (action or "").strip().upper()
    if action == "NO_ACTION":
        return "THOUGHT_ONLY"
    if action in {
        "ASK",
        "READ",
        "WRITE",
        "TEST",
        "EXECUTE",
        "GIT",
        "SUBMIT",
        "THOUGHT_ONLY",
    }:
        return action
    return "OTHER"


def split_action_sequence(row: dict[str, Any]) -> list[str]:
    return [canonical_action_for_phenotype(part) for part in str(row.get("action_sequence") or "").split(",") if part]


def phenotype_family_for_label(label: str) -> str:
    family = model_family(label)
    return family if family in MODEL_FAMILY_ORDER else "Other"


def phenotype_model_rank(model: str) -> int:
    lower = model.lower()
    if "gpt" in lower:
        if "5.5" in lower:
            return 0
        if "5.4" in lower:
            return 1
        if "5.3" in lower:
            return 2
    if "claude" in lower or "opus" in lower:
        if "4.7" in lower:
            return 0
        if "4.6" in lower:
            return 1
    if "gemini" in lower:
        if "3.1" in lower or "3 pro" in lower:
            return 0
    return 50


def phenotype_group_sort_key(label: str, summary_by_label: dict[str, dict[str, Any]]) -> tuple[int, int, str, int, float]:
    summary = summary_by_label.get(label, {})
    model = str(summary.get("model_label") or label.split(" / ")[0])
    scaffold = str(summary.get("scaffold_label") or label.split(" / ")[-1])
    scaffold_rank = 0 if scaffold.startswith("Native") else 1
    return (
        MODEL_FAMILY_ORDER.index(phenotype_family_for_label(model)) if phenotype_family_for_label(model) in MODEL_FAMILY_ORDER else 99,
        phenotype_model_rank(model),
        model,
        scaffold_rank,
        -safe_float(summary.get("pass_at_3"), -1),
    )


def trajectory_action_phenotypes_by_turn(
    rows: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    max_turns: int = 120,
    include_open: bool = False,
    include_idle_end: bool = False,
) -> list[dict[str, Any]]:
    """Aggregate pass-level action sequences into turn-normalized phenotype shares."""
    summary_by_label = {row["group_label"]: row for row in summaries if row["condition"] == "ask_human" and plot_ready(row)}
    included_families = set(MODEL_FAMILY_ORDER if include_open else DEFAULT_PHENOTYPE_FAMILIES)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        label = str(row.get("group_label") or "")
        if row.get("condition") != "ask_human" or label not in summary_by_label:
            continue
        family = phenotype_family_for_label(str(row.get("model_label") or row.get("model") or ""))
        if family not in included_families:
            continue
        groups[label].append(row)

    out: list[dict[str, Any]] = []
    for label in sorted(groups, key=lambda item: phenotype_group_sort_key(item, summary_by_label)):
        group_rows = groups[label]
        summary = summary_by_label[label]
        family = phenotype_family_for_label(str(summary.get("model_label") or ""))
        counts_by_turn: dict[int, Counter[str]] = defaultdict(Counter)
        for row in group_rows:
            sequence = split_action_sequence(row)
            if include_idle_end:
                for turn in range(1, max_turns + 1):
                    action = sequence[turn - 1] if turn <= len(sequence) else "IDLE_END"
                    counts_by_turn[turn][action] += 1
            else:
                for turn, action in enumerate(sequence[:max_turns], start=1):
                    counts_by_turn[turn][action] += 1
        trajectories = len(group_rows)
        action_order = ACTION_PHENOTYPE_WITH_END_LEGEND_ORDER if include_idle_end else ACTION_PHENOTYPE_LEGEND_ORDER
        for turn in range(1, max_turns + 1):
            counts = counts_by_turn[turn]
            active_total = sum(counts.values())
            denominator = trajectories if include_idle_end else active_total
            record: dict[str, Any] = {
                "family": family,
                "group_label": label,
                "model_label": summary.get("model_label"),
                "scaffold_label": summary.get("scaffold_label"),
                "turn_index": turn,
                "total": active_total,
                "trajectories": trajectories,
                "pass_at_3": summary.get("pass_at_3"),
            }
            for action in action_order:
                count = counts.get(action, 0)
                record[action] = count
                record[f"{action}_share"] = count / denominator if denominator else math.nan
            out.append(record)
    return out


def first_turn_for_action(row: dict[str, Any], action_name: str) -> int:
    direct_key = {
        "ASK": "first_ask_turn",
        "WRITE": "first_write_turn",
        "TEST": "first_test_turn",
    }.get(action_name)
    if direct_key:
        direct = safe_int(row.get(direct_key))
        if direct:
            return direct
    sequence = [part for part in str(row.get("action_sequence") or "").split(",") if part]
    return next((idx + 1 for idx, action in enumerate(sequence) if action == action_name), 0)


def question_blocker_integration(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("condition") == "ask_human" and safe_bool(row.get("eval_known")):
            groups[str(row.get("group_label") or "")].append(row)

    out: list[dict[str, Any]] = []
    for label, group_rows in sorted(groups.items()):
        ask_rows = [row for row in group_rows if safe_int(row.get("ask_count") or row.get("num_questions")) > 0]
        q = sum(safe_int(row.get("num_questions") or row.get("ask_count")) for row in group_rows)
        br = sum(safe_int(row.get("num_blockers_resolved")) for row in group_rows)
        bt = sum(safe_int(row.get("num_blockers_total")) for row in group_rows)
        precision, recall, f1 = derived_ask_metrics(q, br, bt)

        wrote_after_ask = 0
        tested_after_ask = 0
        submitted_after_ask = 0
        relevant_unresolved = 0
        relevant_solved = 0
        no_followup_write = 0
        for row in ask_rows:
            first_ask = first_turn_for_action(row, "ASK")
            first_write = first_turn_for_action(row, "WRITE")
            first_test = first_turn_for_action(row, "TEST")
            first_submit = first_turn_for_action(row, "SUBMIT")
            if first_ask and first_write and first_write > first_ask:
                wrote_after_ask += 1
            elif first_ask and not first_write:
                no_followup_write += 1
            if first_ask and first_test and first_test > first_ask:
                tested_after_ask += 1
            if first_ask and first_submit and first_submit > first_ask:
                submitted_after_ask += 1
            if safe_int(row.get("num_blockers_resolved")) > 0:
                if safe_bool(row.get("resolved")):
                    relevant_solved += 1
                else:
                    relevant_unresolved += 1

        denom = len(ask_rows)
        out.append(
            {
                "group_label": label,
                "num_pass_rows": len(group_rows),
                "ask_rows": denom,
                "total_questions": q,
                "total_blockers_resolved": br,
                "total_blockers_total": bt,
                "ask_precision": precision,
                "blocker_recall": recall,
                "ask_f1": f1,
                "asked_then_wrote_rate": wrote_after_ask / denom if denom else math.nan,
                "asked_then_tested_rate": tested_after_ask / denom if denom else math.nan,
                "asked_then_submitted_rate": submitted_after_ask / denom if denom else math.nan,
                "asked_no_followup_write_rate": no_followup_write / denom if denom else math.nan,
                "resolved_after_relevant_ask_rate": relevant_solved / denom if denom else math.nan,
                "unresolved_after_relevant_ask_rate": relevant_unresolved / denom if denom else math.nan,
                "note": "Deterministic proxy only; semantic answer incorporation needs answer-to-patch judging.",
            }
        )
    return out


def median_number(values: list[int]) -> float:
    if not values:
        return math.nan
    values = sorted(values)
    mid = len(values) // 2
    if len(values) % 2:
        return float(values[mid])
    return (values[mid - 1] + values[mid]) / 2


def ask_timing_by_group(rows: list[dict[str, Any]], summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary_by_label = {
        row["group_label"]: row
        for row in summaries
        if row["condition"] == "ask_human" and plot_ready(row)
    }
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        label = str(row.get("group_label") or "")
        if row.get("condition") == "ask_human" and safe_bool(row.get("eval_known")) and label in summary_by_label:
            groups[label].append(row)

    out: list[dict[str, Any]] = []
    for label, group_rows in sorted(groups.items()):
        counts = Counter()
        first_ask_turns: list[int] = []
        first_write_turns: list[int] = []
        for row in group_rows:
            first_ask = safe_int(row.get("first_ask_turn"))
            first_write = safe_int(row.get("first_write_turn"))
            ask_count = safe_int(row.get("ask_count") or row.get("num_questions"))
            if first_ask:
                first_ask_turns.append(first_ask)
            if first_write:
                first_write_turns.append(first_write)

            if not first_ask and ask_count > 0:
                counts["ask_logged_no_action"] += 1
            elif not first_ask:
                counts["no_ask"] += 1
            elif not first_write:
                counts["ask_no_write"] += 1
            elif first_ask < first_write:
                counts["ask_before_write"] += 1
            else:
                counts["ask_after_write"] += 1

        total = len(group_rows)
        summary = summary_by_label[label]
        record: dict[str, Any] = {
            "group_label": label,
            "model_label": summary.get("model_label"),
            "scaffold_label": summary.get("scaffold_label"),
            "num_pass_rows": total,
            "median_first_ask_turn": median_number(first_ask_turns),
            "median_first_write_turn": median_number(first_write_turns),
            "pass_at_3": summary.get("pass_at_3"),
        }
        for bucket in ("ask_before_write", "ask_after_write", "ask_no_write", "ask_logged_no_action", "no_ask"):
            record[bucket] = counts[bucket]
            record[f"{bucket}_share"] = counts[bucket] / total if total else math.nan
        out.append(record)
    return out


BLOCKER_MATCH_STOPWORDS = {
    "about",
    "above",
    "across",
    "after",
    "against",
    "also",
    "before",
    "being",
    "between",
    "could",
    "does",
    "exact",
    "field",
    "fields",
    "from",
    "have",
    "into",
    "must",
    "need",
    "only",
    "provided",
    "requirements",
    "return",
    "returned",
    "should",
    "specific",
    "that",
    "their",
    "there",
    "these",
    "this",
    "value",
    "values",
    "when",
    "where",
    "which",
    "with",
    "without",
}


def normalize_match_text(text: Any) -> str:
    return " ".join(str(text or "").lower().split())


def match_tokens(text: Any) -> set[str]:
    tokens = set()
    for token in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}|\d+", str(text or "").lower()):
        for part in token.split("_"):
            if len(part) >= 4 and part not in BLOCKER_MATCH_STOPWORDS:
                tokens.add(part)
        if len(token) >= 4 and token not in BLOCKER_MATCH_STOPWORDS:
            tokens.add(token)
    return tokens


def token_overlap_score(left: Any, right: Any) -> tuple[float, int]:
    left_tokens = match_tokens(left)
    right_tokens = match_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0, 0
    overlap = len(left_tokens & right_tokens)
    denominator = max(1, min(len(left_tokens), len(right_tokens)))
    return overlap / denominator, overlap


def blocker_lookup_text(blocker: dict[str, Any]) -> str:
    examples = blocker.get("example_questions") or []
    if not isinstance(examples, list):
        examples = [str(examples)]
    return "\n".join(
        [
            str(blocker.get("id") or ""),
            str(blocker.get("description") or ""),
            str(blocker.get("resolution") or ""),
            "\n".join(str(item) for item in examples),
        ]
    )


def answer_matches_blocker(answer: str, blocker: dict[str, Any]) -> tuple[bool, str, float]:
    answer_norm = normalize_match_text(answer)
    resolution = str(blocker.get("resolution") or "")
    resolution_norm = normalize_match_text(resolution)
    if len(answer_norm) >= 35 and len(resolution_norm) >= 35:
        if answer_norm in resolution_norm or resolution_norm in answer_norm:
            return True, "answer_resolution_exact", 1.0
    score, overlap = token_overlap_score(answer, resolution)
    if score >= 0.48 and overlap >= 5:
        return True, "answer_resolution_token", score
    score, overlap = token_overlap_score(answer, blocker_lookup_text(blocker))
    if score >= 0.58 and overlap >= 5:
        return True, "answer_blocker_token", score
    return False, "", max(score, 0.0)


def question_targets_blocker(question: str, blocker: dict[str, Any]) -> tuple[bool, str, float]:
    examples = blocker.get("example_questions") or []
    candidates = [str(blocker.get("description") or ""), str(blocker.get("resolution") or "")]
    if isinstance(examples, list):
        candidates.extend(str(item) for item in examples)
    else:
        candidates.append(str(examples))
    best_score = 0.0
    best_overlap = 0
    for candidate in candidates:
        score, overlap = token_overlap_score(question, candidate)
        if score > best_score or (score == best_score and overlap > best_overlap):
            best_score = score
            best_overlap = overlap
    if best_score >= 0.34 and best_overlap >= 3:
        return True, "question_blocker_token", best_score
    return False, "", best_score


def text_mentions_blocker(text: str, blocker: dict[str, Any]) -> tuple[bool, float]:
    score, overlap = token_overlap_score(text, blocker_lookup_text(blocker))
    return bool(score >= 0.28 and overlap >= 5), score


def load_blocker_registries() -> dict[str, dict[str, Any]]:
    registries: dict[str, dict[str, Any]] = {}
    for root in HIL_BENCH_HARBOR_ROOTS:
        if not root.exists():
            continue
        for metadata_path in sorted(root.glob("swe_*/shared/metadata.json")):
            try:
                metadata = read_json(metadata_path)
            except Exception:
                continue
            attempt_id = str(metadata.get("attempt_id") or "")
            if not attempt_id or attempt_id in registries:
                continue
            registry_path = metadata_path.parent / "ask-human-data" / "blocker_registry.json"
            if not registry_path.exists():
                continue
            try:
                registry = read_json(registry_path)
            except Exception:
                continue
            blockers = registry.get("blockers") if isinstance(registry, dict) else None
            if isinstance(blockers, list):
                registries[attempt_id] = {
                    "task_root": str(metadata_path.parents[1]),
                    "registry_path": str(registry_path),
                    "blockers": [blocker for blocker in blockers if isinstance(blocker, dict)],
                }
    return registries


def ask_events_from_trajectory(trajectory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ask_events: list[dict[str, Any]] = []
    for turn, event in enumerate(trajectory, 1):
        if not isinstance(event, dict):
            continue
        if release_event_type_for_event(event) != "ASK":
            continue
        act = str(event.get("act") or "")
        question = act
        if act.lower().startswith("ask_human"):
            question = act[len("ask_human") :].strip(" :")
        ask_events.append(
            {
                "turn": turn,
                "question": question,
                "answer": str(event.get("obs") or event.get("answer") or ""),
            }
        )
    return ask_events


def trajectory_text_window(trajectory: list[dict[str, Any]], start_turn: int, end_turn: int) -> str:
    chunks: list[str] = []
    for turn, event in enumerate(trajectory, 1):
        if turn < start_turn or turn > end_turn or not isinstance(event, dict):
            continue
        chunks.append(str(event.get("thought") or ""))
        chunks.append(str(event.get("act") or ""))
        chunks.append(str(event.get("obs") or ""))
    return "\n".join(chunks)


def first_concrete_followup_turn(trajectory: list[dict[str, Any]], ask_turn: int) -> tuple[int, int, int]:
    first_write = 0
    first_test = 0
    first_read = 0
    for turn, event in enumerate(trajectory, 1):
        if turn <= ask_turn or not isinstance(event, dict):
            continue
        kind = release_event_type_for_event(event)
        if kind == "WRITE" and not first_write:
            first_write = turn
        elif kind == "TEST" and not first_test:
            first_test = turn
        elif kind == "READ" and not first_read:
            first_read = turn
    return first_write, first_test, first_read


def blocker_lifecycle_proxy(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    registries = load_blocker_registries()
    lifecycle_rows: list[dict[str, Any]] = []
    skipped = Counter()
    for row in rows:
        if row.get("condition") != "ask_human" or not safe_bool(row.get("eval_known")):
            continue
        task_id = str(row.get("task_id") or "")
        trajectory_path = Path(str(row.get("trajectory_path") or ""))
        if not trajectory_path.is_file():
            skipped["missing_json_trajectory"] += 1
            continue
        if trajectory_path.suffix == ".csv":
            skipped["analysis_only_trajectory_csv"] += 1
            continue
        registry = registries.get(task_id)
        if not registry:
            skipped["missing_registry"] += 1
            continue
        try:
            trajectory = coerce_trajectory(read_json(trajectory_path))
        except Exception:
            skipped["trajectory_read_error"] += 1
            continue
        ask_events = ask_events_from_trajectory(trajectory)
        resolved = safe_bool(row.get("resolved"))
        patch_text = ""
        pass_dir = Path(str(row.get("pass_dir") or ""))
        patch_path = pass_dir / "patch.diff"
        if patch_path.exists():
            try:
                patch_text = patch_path.read_text(errors="replace")
            except Exception:
                patch_text = ""

        for blocker in registry["blockers"]:
            blocker_id = str(blocker.get("id") or "")
            blocker_types = blocker.get("type") or []
            blocker_type = ",".join(str(item) for item in blocker_types) if isinstance(blocker_types, list) else str(blocker_types)
            question_turns: list[int] = []
            answer_turns: list[int] = []
            match_methods: list[str] = []
            best_match_score = 0.0
            for ask in ask_events:
                question_ok, question_method, question_score = question_targets_blocker(str(ask["question"]), blocker)
                answer_ok, answer_method, answer_score = answer_matches_blocker(str(ask["answer"]), blocker)
                best_match_score = max(best_match_score, question_score, answer_score)
                if question_ok:
                    question_turns.append(safe_int(ask["turn"]))
                    match_methods.append(question_method)
                if answer_ok:
                    answer_turns.append(safe_int(ask["turn"]))
                    match_methods.append(answer_method)

            first_question_turn = min(question_turns) if question_turns else 0
            first_answer_turn = min(answer_turns) if answer_turns else 0
            first_relevant_turn = first_answer_turn or first_question_turn
            boundary_turn = first_relevant_turn or safe_int(row.get("first_write_turn")) or len(trajectory)
            preboundary_text = trajectory_text_window(trajectory, 1, max(1, boundary_turn - 1))
            discovered_precommit, discovery_score = text_mentions_blocker(preboundary_text, blocker)
            patch_mentions, patch_score = text_mentions_blocker(patch_text, blocker)
            first_write_after_answer = first_test_after_answer = first_read_after_answer = 0
            if first_answer_turn:
                first_write_after_answer, first_test_after_answer, first_read_after_answer = first_concrete_followup_turn(
                    trajectory, first_answer_turn
                )
            answer_received = bool(first_answer_turn)
            question_targeted = bool(first_question_turn)
            concrete_followup = bool(first_write_after_answer or first_test_after_answer)

            if resolved:
                stage = "resolved_run"
            elif answer_received and concrete_followup:
                stage = "answer_received_acted_unresolved"
            elif question_targeted and not answer_received:
                stage = "asked_no_relevant_answer"
            elif answer_received:
                stage = "answer_received_no_followup"
            elif discovered_precommit:
                stage = "discovered_not_asked"
            else:
                stage = "not_detected"

            lifecycle_rows.append(
                {
                    "group_label": row.get("group_label", ""),
                    "model_label": row.get("model_label", ""),
                    "scaffold_label": row.get("scaffold_label", ""),
                    "task_id": task_id,
                    "pass_index": row.get("pass_index", ""),
                    "resolved": resolved,
                    "blocker_id": blocker_id,
                    "blocker_type": blocker_type,
                    "question_targeted": question_targeted,
                    "answer_received": answer_received,
                    "discovered_precommit_proxy": discovered_precommit,
                    "concrete_followup_after_answer": concrete_followup,
                    "patch_mentions_blocker_proxy": patch_mentions,
                    "first_question_turn": first_question_turn,
                    "first_answer_turn": first_answer_turn,
                    "first_write_after_answer": first_write_after_answer,
                    "first_test_after_answer": first_test_after_answer,
                    "first_read_after_answer": first_read_after_answer,
                    "best_match_score": best_match_score,
                    "discovery_score": discovery_score,
                    "patch_score": patch_score,
                    "match_methods": ",".join(sorted(set(match_methods))),
                    "lifecycle_stage_proxy": stage,
                    "registry_path": registry.get("registry_path", ""),
                    "trajectory_path": row.get("trajectory_path", ""),
                    "note": "Blocker-centered deterministic proxy; use LLM judge for semantic discovery/integration claims.",
                }
            )

    summary = summarize_blocker_lifecycle(lifecycle_rows)
    if skipped:
        summary.append(
            {
                "group_label": "__skipped__",
                "lifecycle_stage_proxy": ",".join(f"{key}:{value}" for key, value in sorted(skipped.items())),
                "count": sum(skipped.values()),
                "share": math.nan,
                "blocker_rows": len(lifecycle_rows),
            }
        )
    return lifecycle_rows, summary


def summarize_blocker_lifecycle(lifecycle_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in lifecycle_rows:
        grouped[str(row.get("group_label") or "")].append(row)
    out: list[dict[str, Any]] = []
    for label, group_rows in sorted(grouped.items()):
        total = len(group_rows)
        stage_counts = Counter(str(row.get("lifecycle_stage_proxy") or "") for row in group_rows)
        for stage, count in sorted(stage_counts.items()):
            out.append(
                {
                    "group_label": label,
                    "lifecycle_stage_proxy": stage,
                    "count": count,
                    "share": count / total if total else math.nan,
                    "blocker_rows": total,
                    "question_targeted_share": sum(safe_bool(row.get("question_targeted")) for row in group_rows) / total if total else math.nan,
                    "answer_received_share": sum(safe_bool(row.get("answer_received")) for row in group_rows) / total if total else math.nan,
                    "discovered_precommit_proxy_share": sum(safe_bool(row.get("discovered_precommit_proxy")) for row in group_rows) / total if total else math.nan,
                    "concrete_followup_after_answer_share": sum(safe_bool(row.get("concrete_followup_after_answer")) for row in group_rows) / total if total else math.nan,
                }
            )
    return out


def esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


def svg_text(x: float, y: float, text: Any, size: int = 13, fill: str = "#212529", anchor: str = "start", weight: str = "400") -> str:
    return f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" text-anchor="{anchor}" font-weight="{weight}">{esc(text)}</text>'


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#" + "".join(f"{channel:02x}" for channel in rgb)


def text_fill_for_background(color: str) -> str:
    red, green, blue = hex_to_rgb(color)
    luminance = (0.299 * red + 0.587 * green + 0.114 * blue) / 255
    return "#101828" if luminance > 0.62 else "#ffffff"


def interpolate_hex(left: str, right: str, amount: float) -> str:
    amount = clamp01(amount)
    left_rgb = hex_to_rgb(left)
    right_rgb = hex_to_rgb(right)
    return rgb_to_hex(
        tuple(
            int(round(left_channel + (right_channel - left_channel) * amount))
            for left_channel, right_channel in zip(left_rgb, right_rgb)
        )
    )


def performance_color(value: float, max_value: float) -> str:
    if math.isnan(value):
        return "#868e96"
    if max_value <= 0 or math.isnan(max_value):
        ratio = 0.0
    else:
        ratio = clamp01(value / max_value)
    for idx, (stop, color) in enumerate(PERFORMANCE_COLOR_STOPS[1:], start=1):
        previous_stop, previous_color = PERFORMANCE_COLOR_STOPS[idx - 1]
        if ratio <= stop:
            span = stop - previous_stop
            local_amount = (ratio - previous_stop) / span if span else 0.0
            return interpolate_hex(previous_color, color, local_amount)
    return PERFORMANCE_COLOR_STOPS[-1][1]


def model_family(label: str) -> str:
    lower = (label or "").lower()
    if "claude" in lower:
        return "Claude"
    if "gpt" in lower or "openai" in lower:
        return "GPT"
    if "gemini" in lower:
        return "Gemini"
    if "glm" in lower:
        return "GLM"
    return "Other"


def used_model_families(rows: list[dict[str, Any]]) -> list[str]:
    seen = {model_family(str(row.get("model_label") or row.get("model") or "")) for row in rows}
    return [family for family in MODEL_FAMILY_ORDER if family in seen]


def star_points(cx: float, cy: float, outer_radius: float) -> str:
    inner_radius = outer_radius * 0.46
    points = []
    for idx in range(10):
        radius = outer_radius if idx % 2 == 0 else inner_radius
        angle = math.radians(-90 + idx * 36)
        points.append(f"{cx + math.cos(angle) * radius:.1f},{cy + math.sin(angle) * radius:.1f}")
    return " ".join(points)


def svg_marker(
    cx: float,
    cy: float,
    size: float,
    family: str,
    fill: str,
    stroke: str = "#ffffff",
    stroke_width: float = 2.0,
    opacity: float = 0.92,
) -> str:
    shape = MODEL_FAMILY_SHAPES.get(family, "circle")
    common = f'fill="{fill}" fill-opacity="{opacity:.2f}" stroke="{stroke}" stroke-width="{stroke_width:.1f}"'
    if shape == "star":
        return f'<polygon points="{star_points(cx, cy, size)}" {common} stroke-linejoin="round"/>'
    if shape == "diamond":
        points = f"{cx:.1f},{cy - size:.1f} {cx + size:.1f},{cy:.1f} {cx:.1f},{cy + size:.1f} {cx - size:.1f},{cy:.1f}"
        return f'<polygon points="{points}" {common} stroke-linejoin="round"/>'
    if shape == "triangle":
        points = f"{cx:.1f},{cy - size:.1f} {cx + size * 0.95:.1f},{cy + size * 0.82:.1f} {cx - size * 0.95:.1f},{cy + size * 0.82:.1f}"
        return f'<polygon points="{points}" {common} stroke-linejoin="round"/>'
    if shape == "square":
        half = size * 0.82
        return f'<rect x="{cx - half:.1f}" y="{cy - half:.1f}" width="{half * 2:.1f}" height="{half * 2:.1f}" rx="2" {common}/>'
    return f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{size:.1f}" {common}/>'


def performance_legend(x: float, y: float, width: float, max_value: float, label: str = "Hue = pass@3") -> list[str]:
    body = [svg_text(x, y, label, 12, "#344054", weight="700")]
    segments = 24
    segment_w = width / segments
    for idx in range(segments):
        value = max_value * (idx + 0.5) / segments
        body.append(
            f'<rect x="{x + idx * segment_w:.1f}" y="{y + 9:.1f}" width="{segment_w + 0.6:.1f}" height="9" fill="{performance_color(value, max_value)}"/>'
        )
    body.append(svg_text(x, y + 32, "0%", 11, "#667085"))
    body.append(svg_text(x + width, y + 32, f"{int(round(max_value * 100))}%", 11, "#667085", "end"))
    return body


def shape_legend(families: list[str], x: float, y: float, columns: int = 2) -> list[str]:
    body = [svg_text(x, y, "Shape = model family", 12, "#344054", weight="700")]
    for idx, family in enumerate(families):
        col = idx % columns
        row = idx // columns
        xx = x + col * 92
        yy = y + 24 + row * 24
        body.append(svg_marker(xx + 8, yy - 4, 7.5, family, "#ffffff", "#344054", 1.5, 1.0))
        body.append(svg_text(xx + 22, yy, family, 11, "#344054"))
    return body


def svg(path: Path, width: int, height: int, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    style = """
    <style>
      svg { font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #fbfbf8; }
      .axis { stroke: #343a40; stroke-width: 1.2; }
      .grid { stroke: #dee2e6; stroke-width: 1; }
      .muted { fill: #667085; }
    </style>
    """
    path.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">{style}{body}</svg>\n')


def number_axis_ticks(max_value: float, count: int = 5) -> list[float]:
    if max_value <= 0 or math.isnan(max_value):
        return [0.0, 0.25, 0.5, 0.75, 1.0]
    top = max(0.1, math.ceil(max_value * 10) / 10)
    return [top * i / (count - 1) for i in range(count)]


def ordered_plot_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: (-safe_float(row.get("pass_at_3"), -1), str(row.get("group_label") or "")))


def compact_group_name(row: dict[str, Any]) -> str:
    scaffold = str(row.get("scaffold_label") or "").replace("Native ", "")
    return f"{row.get('model_label')} / {scaffold}"


def plot_same_model_dumbbell(summaries: list[dict[str, Any]], gap_rows: list[dict[str, Any]] | None = None) -> None:
    rows = list(gap_rows) if gap_rows is not None else full_info_gap_rows(summaries)
    width, height = 1280, 760
    margin_l, margin_r, margin_t, margin_b = 112, 380, 150, 98
    plot_w = width - margin_l - margin_r
    plot_h = height - margin_t - margin_b
    max_value = max(
        [safe_float(row.get("ask_human_pass_at_3"), 0.0) for row in rows]
        + [safe_float(row.get("full_info_pass_at_3"), 0.0) for row in rows]
        + [0.85]
    )
    y_ticks = number_axis_ticks(max_value, 7)
    axis_max = y_ticks[-1]
    x_axis_min = 0.70
    x_axis_max = axis_max
    x_ticks = [tick / 100 for tick in range(int(x_axis_min * 100), int(x_axis_max * 100) + 1, 5)]

    def x(value: float) -> float:
        span = x_axis_max - x_axis_min
        return margin_l + ((value - x_axis_min) / span) * plot_w if span else margin_l

    def y(value: float) -> float:
        return margin_t + (1 - value / axis_max) * plot_h

    optimal_color = "#f59e0b"
    real_color = "#2563eb"
    body = []
    body.append(svg_text(48, 42, "FullInfo vs AskHuman Pass@3", 28, "#101828", weight="700"))
    body.append(svg_text(48, 68, "Matched native scaffolds on intersected tasks; gold marks no drop-off, blue marks observed HIL-Bench.", 15, "#667085"))
    rail_x = width - margin_r + 46
    body.append(svg_text(rail_x, 30, "Color = condition", 12, "#344054", weight="700"))
    body.append(svg_marker(rail_x + 9, 56, 8, "GPT", optimal_color, "#ffffff", 1.6, 0.96))
    body.append(svg_text(rail_x + 26, 60, "gold/orange = no drop-off", 11, "#667085"))
    body.append(svg_marker(rail_x + 154, 56, 8, "GPT", real_color, "#ffffff", 1.6, 0.96))
    body.append(svg_text(rail_x + 171, 60, "blue = HIL-Bench", 11, "#667085"))
    body.extend(shape_legend(used_model_families([{"model_label": row.get("model_label")} for row in rows]), rail_x, 98))

    body.append(
        f'<polygon points="{x(x_axis_min):.1f},{y(0):.1f} {x(x_axis_min):.1f},{y(x_axis_min):.1f} '
        f'{x(x_axis_max):.1f},{y(x_axis_max):.1f} {x(x_axis_max):.1f},{y(0):.1f}" '
        'fill="#fff1f0" fill-opacity="0.58"/>'
    )
    body.append(f'<line class="axis" x1="{margin_l}" y1="{height-margin_b}" x2="{width-margin_r}" y2="{height-margin_b}"/>')
    body.append(f'<line class="axis" x1="{margin_l}" y1="{margin_t}" x2="{margin_l}" y2="{height-margin_b}"/>')
    for tick in x_ticks:
        xx = x(tick)
        body.append(f'<line class="grid" x1="{xx:.1f}" y1="{margin_t}" x2="{xx:.1f}" y2="{height-margin_b}"/>')
        body.append(svg_text(xx, height - margin_b + 28, f"{int(round(tick * 100))}%", 12, "#667085", "middle"))
    for tick in y_ticks:
        yy = y(tick)
        body.append(f'<line class="grid" x1="{margin_l}" y1="{yy:.1f}" x2="{width-margin_r}" y2="{yy:.1f}"/>')
        body.append(svg_text(margin_l - 18, yy + 4, f"{int(round(tick * 100))}%", 12, "#667085", "end"))
    body.append(
        f'<line x1="{x(x_axis_min):.1f}" y1="{y(x_axis_min):.1f}" x2="{x(x_axis_max):.1f}" y2="{y(x_axis_max):.1f}" '
        'stroke="#98a2b3" stroke-width="1.6" stroke-dasharray="6 6"/>'
    )
    body.append(svg_text(x(0.80), y(0.80) - 12, "no drop-off", 12, "#667085", "middle", "700"))
    body.append(svg_text(x(0.80), y(axis_max * 0.28), "context drop-off region", 12, "#b42318", "middle", "700"))
    body.append(svg_text(margin_l + plot_w / 2, height - 34, "FullInfo pass@3", 14, "#344054", "middle", "700"))
    body.append(svg_text(margin_l, margin_t - 22, "AskHuman pass@3", 14, "#344054", "start", "700"))

    if not rows:
        body.append(svg_text(width / 2, height / 2, "No matched AskHuman/FullInfo native rows found.", 20, "#667085", "middle", "600"))
        svg(FIG_DIR / "01_same_model_different_scaffold.svg", width, height, "\n".join(body))
        return

    indexed_rows = sorted(
        rows,
        key=lambda row: (
            -safe_float(row.get("full_info_minus_ask_human_pp"), -1),
            str(row.get("group_label") or ""),
        ),
    )
    for row in indexed_rows:
        ask = safe_float(row.get("ask_human_pass_at_3"), math.nan)
        full = safe_float(row.get("full_info_pass_at_3"), math.nan)
        if math.isnan(ask) or math.isnan(full):
            continue
        xx = x(full)
        yy = y(ask)
        diagonal_y = y(full)
        family = model_family(str(row.get("model_label") or ""))
        body.append(f'<line x1="{xx:.1f}" y1="{diagonal_y:.1f}" x2="{xx:.1f}" y2="{yy:.1f}" stroke="#98a2b3" stroke-width="2.2" stroke-linecap="round"/>')
        body.append(svg_marker(xx, diagonal_y, 11, family, optimal_color, "#ffffff", 2.4, 0.96))
        body.append(svg_marker(xx, yy, 14, family, real_color, "#ffffff", 3.0, 0.96))

    table_top = 230
    body.append(svg_text(rail_x, table_top, "Largest native context gaps", 14, "#344054", weight="700"))
    body.append(svg_text(rail_x, table_top + 21, "Drop = FullInfo pass@3 minus AskHuman pass@3", 11, "#667085"))
    for idx, row in enumerate(indexed_rows, start=1):
        ask = safe_float(row.get("ask_human_pass_at_3"), math.nan)
        full = safe_float(row.get("full_info_pass_at_3"), math.nan)
        gap = safe_float(row.get("full_info_minus_ask_human_pp"), math.nan)
        if math.isnan(ask) or math.isnan(full):
            continue
        yy = table_top + 60 + (idx - 1) * 78
        family = model_family(str(row.get("model_label") or ""))
        scaffold = str(row.get("scaffold_label") or "").replace("Native ", "")
        n_text = f"n={row.get('num_intersected_tasks')} intersected tasks" if row.get("num_intersected_tasks") else f"n={row.get('ask_human_pass_at_3_n')} AskHuman tasks"
        body.append(f'<line x1="{rail_x:.1f}" y1="{yy - 36:.1f}" x2="{width - 52:.1f}" y2="{yy - 36:.1f}" stroke="#e4e7ec" stroke-width="1"/>')
        body.append(svg_marker(rail_x + 12, yy - 8, 8.5, family, real_color, "#ffffff", 1.7, 0.96))
        body.append(svg_text(rail_x + 30, yy - 14, f"{idx}. {row.get('model_label')} / {scaffold}", 12, "#101828", weight="700"))
        body.append(svg_text(rail_x + 30, yy + 4, f"Ask {pct(ask)}   Full {pct(full)}   drop {gap:.1f} pp", 11, "#667085"))
        body.append(svg_text(rail_x + 30, yy + 21, n_text, 10, "#98a2b3"))

    svg(FIG_DIR / "01_same_model_different_scaffold.svg", width, height, "\n".join(body))


def plot_full_info_gap(gap_rows: list[dict[str, Any]]) -> None:
    rows = list(gap_rows)
    width = 1080
    row_h = 78
    margin_l, margin_r, margin_t, margin_b = 250, 106, 128, 86
    height = margin_t + row_h * max(len(rows), 1) + margin_b
    plot_w = width - margin_l - margin_r
    max_x = max(
        [safe_float(row.get("ask_human_pass_at_3"), 0.0) for row in rows]
        + [safe_float(row.get("full_info_pass_at_3"), 0.0) for row in rows]
        + [0.85]
    )
    ticks = number_axis_ticks(max_x, 6)
    x_max = ticks[-1]

    def x(value: float) -> float:
        return margin_l + (value / x_max) * plot_w

    body: list[str] = []
    body.append(svg_text(48, 42, "FullInfo Ceiling Gap", 28, "#101828", weight="700"))
    body.append(svg_text(48, 68, "Matched native scaffolds: performance with all blockers revealed vs AskHuman.", 15, "#667085"))
    ask_color = "#2563eb"
    full_color = "#2f9e44"
    body.append(f'<circle cx="52" cy="105" r="7" fill="{ask_color}" stroke="#ffffff" stroke-width="1.6"/>')
    body.append(svg_text(66, 109, "AskHuman", 12, "#344054"))
    body.append(f'<circle cx="150" cy="105" r="7" fill="{full_color}" stroke="#ffffff" stroke-width="1.6"/>')
    body.append(svg_text(164, 109, "FullInfo", 12, "#344054"))
    body.append(svg_text(width - 48, 105, "gap", 12, "#344054", "end", "700"))

    axis_y0 = margin_t - 6
    axis_y1 = height - margin_b + 4
    for tick in ticks:
        xx = x(tick)
        body.append(f'<line class="grid" x1="{xx:.1f}" y1="{axis_y0}" x2="{xx:.1f}" y2="{axis_y1}"/>')
        body.append(svg_text(xx, height - margin_b + 30, f"{int(round(tick * 100))}%", 12, "#667085", "middle"))
    body.append(f'<line class="axis" x1="{margin_l}" y1="{height-margin_b}" x2="{width-margin_r}" y2="{height-margin_b}"/>')
    body.append(svg_text(margin_l + plot_w / 2, height - 28, "pass@3", 13, "#344054", "middle", "600"))

    if not rows:
        body.append(svg_text(width / 2, height / 2, "No matched AskHuman/FullInfo native rows found.", 16, "#667085", "middle", "600"))
        svg(FIG_DIR / "12_full_info_gap.svg", width, height, "\n".join(body))
        return

    for idx, row in enumerate(rows):
        y = margin_t + idx * row_h
        ask = safe_float(row.get("ask_human_pass_at_3"), math.nan)
        full = safe_float(row.get("full_info_pass_at_3"), math.nan)
        low, high = min(ask, full), max(ask, full)
        label = f'{row.get("model_label")} / {str(row.get("scaffold_label")).replace("Native ", "")}'
        body.append(svg_text(48, y + 5, label, 13, "#101828", weight="700"))
        body.append(svg_text(48, y + 25, f"AskHuman n={row.get('ask_human_pass_at_3_n')} | FullInfo n={row.get('full_info_pass_at_3_n')}", 11, "#667085"))
        body.append(f'<line x1="{x(low):.1f}" y1="{y:.1f}" x2="{x(high):.1f}" y2="{y:.1f}" stroke="#98a2b3" stroke-width="4" stroke-linecap="round"/>')
        family = model_family(str(row.get("model_label") or ""))
        body.append(svg_marker(x(ask), y, 11.5, family, ask_color, "#ffffff", 2.4, 0.94))
        body.append(svg_marker(x(full), y, 11.5, family, full_color, "#ffffff", 2.4, 0.94))
        body.append(svg_text(x(ask), y - 19, pct(ask), 11, ask_color, "middle", "700"))
        body.append(svg_text(x(full), y + 34, pct(full), 11, full_color, "middle", "700"))
        gap = safe_float(row.get("full_info_minus_ask_human_pp"), math.nan)
        body.append(svg_text(width - 48, y + 5, f"+{gap:.1f} pp" if not math.isnan(gap) else "n/a", 13, "#344054", "end", "700"))

    svg(FIG_DIR / "12_full_info_gap.svg", width, height, "\n".join(body))


def plot_detection_targeting(summaries: list[dict[str, Any]]) -> None:
    rows = [row for row in summaries if row["condition"] == "ask_human" and plot_ready(row)]
    width, height = 1080, 760
    margin_l, margin_r, margin_t, margin_b = 105, 95, 140, 96
    plot_w = width - margin_l - margin_r
    plot_h = height - margin_t - margin_b
    performance_max = number_axis_ticks(max([safe_float(row["pass_at_3"]) for row in rows] + [0.35]), 6)[-1]

    def x(value: float) -> float:
        return margin_l + value * plot_w

    def y(value: float) -> float:
        return margin_t + (1 - value) * plot_h

    body = []
    body.append(svg_text(48, 42, "Detection vs Targeting", 28, "#101828", weight="700"))
    body.append(svg_text(48, 68, "Recall measures blocker detection; precision measures signal-to-noise under a finite human budget.", 15, "#667085"))
    legend_x = width - 330
    body.extend(performance_legend(legend_x, 32, 170, performance_max))
    body.extend(shape_legend(used_model_families(rows), legend_x, 84))
    for tick in [0, 0.25, 0.5, 0.75, 1.0]:
        xx = x(tick)
        yy = y(tick)
        body.append(f'<line class="grid" x1="{xx:.1f}" y1="{margin_t}" x2="{xx:.1f}" y2="{height-margin_b}"/>')
        body.append(f'<line class="grid" x1="{margin_l}" y1="{yy:.1f}" x2="{width-margin_r}" y2="{yy:.1f}"/>')
        body.append(svg_text(xx, height - margin_b + 28, f"{int(tick * 100)}%", 12, "#667085", "middle"))
        body.append(svg_text(margin_l - 18, yy + 4, f"{int(tick * 100)}%", 12, "#667085", "end"))
    body.append(f'<line class="axis" x1="{margin_l}" y1="{height-margin_b}" x2="{width-margin_r}" y2="{height-margin_b}"/>')
    body.append(f'<line class="axis" x1="{margin_l}" y1="{margin_t}" x2="{margin_l}" y2="{height-margin_b}"/>')
    body.append(svg_text(margin_l + plot_w / 2, height - 34, "Ask precision", 14, "#344054", "middle", "700"))
    body.append(svg_text(margin_l, margin_t - 22, "Blocker recall", 14, "#344054", "start", "700"))
    body.append(svg_text(x(0.17), y(0.14), "silent guessers", 14, "#98a2b3", "middle", "700"))
    body.append(svg_text(x(0.18), y(0.86), "noisy detectors", 14, "#98a2b3", "middle", "700"))
    body.append(svg_text(x(0.82), y(0.86), "calibrated", 14, "#98a2b3", "middle", "700"))

    label_offsets = {
        "GPT-5.5 / SWE-agent": (16, 28, "start"),
        "Gemini 3.1 Pro / Native ADK": (-18, -28, "end"),
        "GPT-5.5 / Native Codex": (-16, -18, "end"),
        "GPT-5.5 / Native Codex Tool": (12, -22, "start"),
        "Claude Opus 4.7 / Native Claude Code": (16, -10, "start"),
        "Claude Opus 4.7 / Native Claude Code Tool": (16, 32, "start"),
        "Claude Opus 4.7 / SWE-agent": (-16, -10, "end"),
        "Gemini 3.1 Pro / SWE-agent": (-16, -22, "end"),
        "GLM-5P1 / Native OpenCode": (16, -22, "start"),
        "GPT-5.4 / SWE-agent": (-16, -8, "end"),
    }
    for idx, row in enumerate(sorted(rows, key=lambda row: row["group_label"])):
        precision = max(0.0, min(1.0, safe_float(row["ask_precision"], 0.0)))
        recall = max(0.0, min(1.0, safe_float(row["ask_recall"], 0.0)))
        pass3 = safe_float(row["pass_at_3"], 0.0)
        marker_size = 13.5
        color = performance_color(pass3, performance_max)
        family = model_family(str(row["model_label"]))
        xx = x(precision)
        yy = y(recall)
        body.append(svg_marker(xx, yy, marker_size, family, color))
        dx, dy, anchor = label_offsets.get(row["group_label"], (12 if idx % 2 == 0 else -12, -marker_size - 8, "start" if idx % 2 == 0 else "end"))
        body.append(svg_text(xx + dx, yy + dy, f'{row["model_label"]} / {row["scaffold_label"].replace("Native ", "")}', 12, "#101828", anchor, "700"))
        if math.isnan(pass3):
            body.append(svg_text(xx + dx, yy + dy + 16, "pass@3 n/a", 11, "#667085", anchor))
        else:
            body.append(svg_text(xx + dx, yy + dy + 16, f"pass@3 {pct(pass3)}", 11, "#667085", anchor))

    svg(FIG_DIR / "02_detection_targeting.svg", width, height, "\n".join(body))


def plot_pass_vs_ask_burden(summaries: list[dict[str, Any]]) -> None:
    rows = [row for row in summaries if row["condition"] == "ask_human" and plot_ready(row)]
    width, height = 1080, 720
    margin_l, margin_r, margin_t, margin_b = 105, 90, 128, 92
    plot_w = width - margin_l - margin_r
    plot_h = height - margin_t - margin_b
    max_pass = max([safe_float(row["pass_at_3"]) for row in rows] + [0.35])
    x_ticks = [0.0, 0.25, 0.5, 0.75, 1.0]
    y_ticks = number_axis_ticks(max_pass, 6)
    y_max = y_ticks[-1]

    def x(value: float) -> float:
        return margin_l + clamp01(value) * plot_w

    def y(value: float) -> float:
        return margin_t + (1 - value / y_max) * plot_h

    body = []
    body.append(svg_text(48, 42, "Pass@3 vs Ask Precision", 28, "#101828", weight="700"))
    body.append(svg_text(48, 68, "Precision normalizes question volume by whether asks hit real blockers.", 15, "#667085"))
    legend_x = width - 330
    body.extend(performance_legend(legend_x, 30, 170, y_max))
    body.extend(shape_legend(used_model_families(rows), legend_x, 82))
    for tick in x_ticks:
        xx = x(tick)
        body.append(f'<line class="grid" x1="{xx:.1f}" y1="{margin_t}" x2="{xx:.1f}" y2="{height-margin_b}"/>')
        body.append(svg_text(xx, height - margin_b + 28, f"{int(round(tick * 100))}%", 12, "#667085", "middle"))
    for tick in y_ticks:
        yy = y(tick)
        body.append(f'<line class="grid" x1="{margin_l}" y1="{yy:.1f}" x2="{width-margin_r}" y2="{yy:.1f}"/>')
        body.append(svg_text(margin_l - 18, yy + 4, f"{int(round(tick * 100))}%", 12, "#667085", "end"))
    body.append(f'<line class="axis" x1="{margin_l}" y1="{height-margin_b}" x2="{width-margin_r}" y2="{height-margin_b}"/>')
    body.append(f'<line class="axis" x1="{margin_l}" y1="{margin_t}" x2="{margin_l}" y2="{height-margin_b}"/>')
    body.append(svg_text(margin_l + plot_w / 2, height - 34, "Ask precision", 14, "#344054", "middle", "700"))
    body.append(svg_text(margin_l, margin_t - 22, "AskHuman pass@3", 14, "#344054", "start", "700"))

    for idx, row in enumerate(sorted(rows, key=lambda row: row["group_label"])):
        precision = safe_float(row["ask_precision"], 0.0)
        xx = x(precision)
        pass3 = safe_float(row["pass_at_3"], 0.0)
        yy = y(pass3)
        color = performance_color(pass3, y_max)
        family = model_family(str(row["model_label"]))
        asks_per_pass = safe_float(row["avg_questions_per_pass"], 0.0)
        marker_size = 13.5
        body.append(svg_marker(xx, yy, marker_size, family, color))
        if xx > margin_l + plot_w * 0.72:
            dx, anchor = -12, "end"
        else:
            dx, anchor = 12, "start"
        body.append(svg_text(xx + dx, yy - marker_size - 8, f'{row["model_label"]} / {row["scaffold_label"].replace("Native ", "")}', 12, "#101828", anchor, "700"))
        body.append(svg_text(xx + dx, yy - marker_size + 8, f"asks/pass {asks_per_pass:.2f}", 11, "#667085", anchor))

    svg(FIG_DIR / "03_pass_vs_ask_burden.svg", width, height, "\n".join(body))


def plot_ask_funnel(summaries: list[dict[str, Any]]) -> None:
    rows = ordered_plot_rows([row for row in summaries if row["condition"] == "ask_human" and plot_ready(row)])
    width = 1240
    row_h = 64
    margin_t = 150
    margin_b = 70
    height = margin_t + row_h * len(rows) + margin_b
    label_x = 48
    blocker_x = 345
    ask_x = 710
    bar_w = 285
    bar_h = 18
    metric_x = 1050
    colors = {
        "resolved": "#2f9e44",
        "missed": "#e9ecef",
        "useful": "#2563eb",
        "unproductive": "#f8dcc2",
    }

    body: list[str] = []
    body.append(svg_text(48, 42, "Ask Funnel", 28, "#101828", weight="700"))
    body.append(svg_text(48, 68, "Agents often ask useful questions when they ask; the larger loss is missed blockers.", 15, "#667085"))
    body.append(svg_text(blocker_x, 114, "Blocker recall", 13, "#344054", weight="700"))
    body.append(svg_text(ask_x, 114, "Ask precision", 13, "#344054", weight="700"))
    body.append(svg_text(metric_x, 114, "pass@3", 13, "#344054", weight="700"))
    for base_x in (blocker_x, ask_x):
        for tick in (0, 0.5, 1.0):
            xx = base_x + tick * bar_w
            body.append(f'<line class="grid" x1="{xx:.1f}" y1="{126}" x2="{xx:.1f}" y2="{height - margin_b + 8}"/>')
            body.append(svg_text(xx, 136, f"{int(tick * 100)}%", 11, "#667085", "middle"))

    legend_y = height - 34
    legend = [
        ("blockers found", colors["resolved"]),
        ("blockers missed", colors["missed"]),
        ("productive asks", colors["useful"]),
        ("non-blocker asks", colors["unproductive"]),
    ]
    lx = 48
    for label, color in legend:
        body.append(f'<rect x="{lx}" y="{legend_y - 11}" width="13" height="13" fill="{color}" stroke="#d0d5dd" stroke-width="0.5"/>')
        body.append(svg_text(lx + 18, legend_y, label, 12, "#344054"))
        lx += max(126, 7.5 * len(label) + 38)

    for idx, row in enumerate(rows):
        y = margin_t + idx * row_h
        label = compact_group_name(row)
        body.append(svg_text(label_x, y + 3, label, 12, "#101828", weight="700"))
        body.append(svg_text(label_x, y + 21, f"asks/pass {safe_float(row.get('avg_questions_per_pass'), 0):.2f}", 11, "#667085"))

        recall = clamp01(safe_float(row.get("ask_recall"), 0.0))
        precision = clamp01(safe_float(row.get("ask_precision"), 0.0))
        blocker_y = y - 4
        ask_y = y + 20
        for base_x, yy, value, left_color, right_color, label_text in (
            (blocker_x, blocker_y, recall, colors["resolved"], colors["missed"], pct(recall)),
            (ask_x, ask_y, precision, colors["useful"], colors["unproductive"], pct(precision)),
        ):
            body.append(f'<rect x="{base_x}" y="{yy}" width="{bar_w}" height="{bar_h}" fill="{right_color}" stroke="#d0d5dd" stroke-width="0.6"/>')
            body.append(f'<rect x="{base_x}" y="{yy}" width="{value * bar_w:.1f}" height="{bar_h}" fill="{left_color}"/>')
            if value >= 0.18:
                body.append(svg_text(base_x + value * bar_w - 8, yy + 13, label_text, 11, text_fill_for_background(left_color), "end", "700"))
            else:
                body.append(svg_text(base_x + value * bar_w + 6, yy + 13, label_text, 11, "#344054", "start", "700"))

        pass3 = safe_float(row.get("pass_at_3"), math.nan)
        body.append(svg_text(metric_x, y + 14, pct(pass3), 12, "#101828", weight="700"))

    svg(FIG_DIR / "07_ask_funnel.svg", width, height, "\n".join(body))


def plot_first_ask_timing(timing_rows: list[dict[str, Any]], summaries: list[dict[str, Any]]) -> None:
    summary_by_label = {row["group_label"]: row for row in summaries if row["condition"] == "ask_human" and plot_ready(row)}
    rows = ordered_plot_rows([row for row in timing_rows if row["group_label"] in summary_by_label])
    width = 1240
    row_h = 54
    margin_l, margin_t, margin_b = 295, 142, 74
    margin_r = 72
    height = margin_t + row_h * len(rows) + margin_b
    plot_w = width - margin_l - margin_r
    bar_h = 24
    buckets = [
        ("ask_before_write", "ask before edit", "#2563eb"),
        ("ask_after_write", "ask after edit", "#f59e0b"),
        ("ask_no_write", "asked, no edit", "#94a3b8"),
        ("ask_logged_no_action", "ask metadata only", "#c084fc"),
        ("no_ask", "no ask", "#e5e7eb"),
    ]

    body: list[str] = []
    body.append(svg_text(48, 42, "When Do Agents Ask?", 28, "#101828", weight="700"))
    body.append(svg_text(48, 68, "Share of passes by first ask relative to first edit.", 15, "#667085"))
    legend_x = 48
    legend_y = 108
    for bucket, label, color in buckets:
        item_w = max(120, 7.5 * len(label) + 38)
        body.append(f'<rect x="{legend_x}" y="{legend_y - 11}" width="13" height="13" fill="{color}" stroke="#d0d5dd" stroke-width="0.5"/>')
        body.append(svg_text(legend_x + 18, legend_y, label, 12, "#344054"))
        legend_x += item_w
    for tick in (0, 0.25, 0.5, 0.75, 1.0):
        xx = margin_l + tick * plot_w
        body.append(f'<line class="grid" x1="{xx:.1f}" y1="{margin_t - 12}" x2="{xx:.1f}" y2="{height - margin_b + 6}"/>')
        body.append(svg_text(xx, margin_t - 20, f"{int(tick * 100)}%", 11, "#667085", "middle"))

    for idx, row in enumerate(rows):
        y = margin_t + idx * row_h
        label = compact_group_name(row)
        body.append(svg_text(48, y + 7, label, 12, "#101828", weight="700"))
        ask_med = safe_float(row.get("median_first_ask_turn"), math.nan)
        write_med = safe_float(row.get("median_first_write_turn"), math.nan)
        med_label = f"median ask {ask_med:.0f}, edit {write_med:.0f}" if not math.isnan(ask_med) and not math.isnan(write_med) else "median unavailable"
        body.append(svg_text(48, y + 25, med_label, 11, "#667085"))
        x0 = margin_l
        yy = y - 11
        for bucket, _, color in buckets:
            share = safe_float(row.get(f"{bucket}_share"), 0.0)
            w = share * plot_w
            if w <= 0.1:
                continue
            body.append(f'<rect x="{x0:.1f}" y="{yy:.1f}" width="{w:.1f}" height="{bar_h}" fill="{color}"/>')
            if share >= 0.12:
                body.append(svg_text(x0 + w / 2, yy + 16, f"{int(round(share * 100))}%", 11, text_fill_for_background(color), "middle", "700"))
            if x0 > margin_l and w > 2:
                body.append(f'<line x1="{x0:.1f}" y1="{yy:.1f}" x2="{x0:.1f}" y2="{yy + bar_h:.1f}" stroke="#fbfbf8" stroke-width="1.2"/>')
            x0 += w
    svg(FIG_DIR / "08_first_ask_timing.svg", width, height, "\n".join(body))


def plot_question_follow_through(integration_rows: list[dict[str, Any]], summaries: list[dict[str, Any]]) -> None:
    summary_by_label = {row["group_label"]: row for row in summaries if row["condition"] == "ask_human" and plot_ready(row)}
    merged: list[dict[str, Any]] = []
    for row in integration_rows:
        label = str(row.get("group_label") or "")
        if label not in summary_by_label:
            continue
        merged_row = dict(row)
        merged_row.update(
            {
                "model_label": summary_by_label[label].get("model_label"),
                "scaffold_label": summary_by_label[label].get("scaffold_label"),
                "pass_at_3": summary_by_label[label].get("pass_at_3"),
            }
        )
        merged.append(merged_row)
    rows = ordered_plot_rows(merged)
    width = 1240
    row_h = 56
    margin_l, margin_t, margin_b = 305, 150, 78
    margin_r = 88
    height = margin_t + row_h * len(rows) + margin_b
    plot_w = width - margin_l - margin_r
    metrics = [
        ("asked_then_wrote_rate", "wrote after ask", "#2563eb", -9),
        ("asked_then_tested_rate", "tested after ask", "#16a34a", 0),
        ("resolved_after_relevant_ask_rate", "relevant ask + resolved", "#7c3aed", 9),
    ]

    body: list[str] = []
    body.append(svg_text(48, 42, "Question Follow-through", 28, "#101828", weight="700"))
    body.append(svg_text(48, 68, "Deterministic proxies: after asking, does the run edit, test, or resolve?", 15, "#667085"))
    legend_x = 48
    legend_y = 112
    for _, label, color, _ in metrics:
        body.append(f'<circle cx="{legend_x + 7:.1f}" cy="{legend_y - 5:.1f}" r="6" fill="{color}" stroke="#ffffff" stroke-width="1.5"/>')
        body.append(svg_text(legend_x + 18, legend_y, label, 12, "#344054"))
        legend_x += max(156, 7.5 * len(label) + 38)
    for tick in (0, 0.25, 0.5, 0.75, 1.0):
        xx = margin_l + tick * plot_w
        body.append(f'<line class="grid" x1="{xx:.1f}" y1="{margin_t - 16}" x2="{xx:.1f}" y2="{height - margin_b + 8}"/>')
        body.append(svg_text(xx, margin_t - 24, f"{int(tick * 100)}%", 11, "#667085", "middle"))

    for idx, row in enumerate(rows):
        y = margin_t + idx * row_h
        label = compact_group_name(row)
        body.append(svg_text(48, y + 4, label, 12, "#101828", weight="700"))
        body.append(svg_text(48, y + 22, f"ask rows {safe_int(row.get('ask_rows'))}", 11, "#667085"))
        body.append(f'<line x1="{margin_l}" y1="{y:.1f}" x2="{margin_l + plot_w}" y2="{y:.1f}" stroke="#d0d5dd" stroke-width="1.1"/>')
        for key, _, color, offset in metrics:
            value = clamp01(safe_float(row.get(key), 0.0))
            xx = margin_l + value * plot_w
            yy = y + offset
            body.append(f'<circle cx="{xx:.1f}" cy="{yy:.1f}" r="7" fill="{color}" stroke="#ffffff" stroke-width="1.8"/>')
            if key == "resolved_after_relevant_ask_rate":
                body.append(svg_text(xx + 11, yy + 4, pct(value), 11, "#344054", weight="700"))
    svg(FIG_DIR / "09_question_follow_through.svg", width, height, "\n".join(body))


def plot_same_model_harness_failure_profile(summaries: list[dict[str, Any]]) -> None:
    rows = [row for row in summaries if row["condition"] == "ask_human" and plot_ready(row)]
    by_model: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        scaffold = str(row.get("scaffold_label") or "")
        if scaffold == "SWE-agent":
            by_model[str(row.get("model_label") or "")]["SWE-agent"] = row
        elif scaffold.startswith("Native "):
            by_model[str(row.get("model_label") or "")]["Native"] = row

    pairs = []
    for model, model_rows in by_model.items():
        if "Native" in model_rows and "SWE-agent" in model_rows:
            pairs.append((model, model_rows["Native"], model_rows["SWE-agent"]))
    pairs.sort(key=lambda item: (-safe_float(item[1].get("pass_at_3"), -1), item[0]))

    width = 1240
    row_h = 96
    margin_t = 140
    margin_b = 84
    height = margin_t + row_h * len(pairs) + margin_b
    label_x = 48
    axis_w = 210
    metric_xs = [410, 675, 940]
    metrics = [
        ("pass_at_3", "pass@3"),
        ("ask_precision", "ask precision"),
        ("ask_recall", "blocker recall"),
    ]
    native_color = "#2563eb"
    swe_color = "#f59e0b"

    body: list[str] = []
    body.append(svg_text(48, 42, "Same Model, Different Harness Failures", 28, "#101828", weight="700"))
    body.append(svg_text(48, 68, "Paired profiles show the harness changes both performance and asking behavior.", 15, "#667085"))
    body.append(f'<circle cx="52" cy="105" r="7" fill="{native_color}" stroke="#ffffff" stroke-width="1.6"/>')
    body.append(svg_text(66, 109, "Native harness", 12, "#344054"))
    body.append(f'<circle cx="190" cy="105" r="7" fill="{swe_color}" stroke="#ffffff" stroke-width="1.6"/>')
    body.append(svg_text(204, 109, "SWE-agent", 12, "#344054"))

    for (key, metric_label), x0 in zip(metrics, metric_xs):
        body.append(svg_text(x0, 114, metric_label, 13, "#344054", weight="700"))
        for tick in (0, 0.5, 1.0):
            xx = x0 + tick * axis_w
            body.append(f'<line class="grid" x1="{xx:.1f}" y1="{margin_t - 8}" x2="{xx:.1f}" y2="{height - margin_b + 4}"/>')
            body.append(svg_text(xx, 132, f"{int(tick * 100)}%", 11, "#667085", "middle"))

    for idx, (model, native_row, swe_row) in enumerate(pairs):
        y = margin_t + idx * row_h
        body.append(svg_text(label_x, y + 12, model, 14, "#101828", weight="700"))
        body.append(svg_text(label_x, y + 32, str(native_row.get("scaffold_label")).replace("Native ", ""), 11, native_color, weight="700"))
        body.append(svg_text(label_x, y + 49, "SWE-agent", 11, swe_color, weight="700"))
        for key, _ in metrics:
            x0 = metric_xs[[metric[0] for metric in metrics].index(key)]
            native_v = clamp01(safe_float(native_row.get(key), 0.0))
            swe_v = clamp01(safe_float(swe_row.get(key), 0.0))
            nx = x0 + native_v * axis_w
            sx = x0 + swe_v * axis_w
            body.append(f'<line x1="{nx:.1f}" y1="{y + 26:.1f}" x2="{sx:.1f}" y2="{y + 46:.1f}" stroke="#98a2b3" stroke-width="1.4"/>')
            body.append(f'<circle cx="{nx:.1f}" cy="{y + 26:.1f}" r="7" fill="{native_color}" stroke="#ffffff" stroke-width="1.8"/>')
            body.append(f'<circle cx="{sx:.1f}" cy="{y + 46:.1f}" r="7" fill="{swe_color}" stroke="#ffffff" stroke-width="1.8"/>')
            body.append(svg_text(nx, y + 15, pct(native_v), 10, "#344054", "middle", "700"))
            body.append(svg_text(sx, y + 63, pct(swe_v), 10, "#344054", "middle", "700"))

    svg(FIG_DIR / "10_same_model_harness_failure_profile.svg", width, height, "\n".join(body))


def plot_blocker_lifecycle_proxy(lifecycle_summary_rows: list[dict[str, Any]], summaries: list[dict[str, Any]]) -> None:
    summary_by_label = {row["group_label"]: row for row in summaries if row["condition"] == "ask_human" and plot_ready(row)}
    by_group_stage: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    group_stats: dict[str, dict[str, Any]] = {}
    for row in lifecycle_summary_rows:
        label = str(row.get("group_label") or "")
        stage = str(row.get("lifecycle_stage_proxy") or "")
        if not label or label == "__skipped__" or label not in summary_by_label:
            continue
        by_group_stage[label][stage] = row
        group_stats.setdefault(label, row)

    labels = sorted(
        by_group_stage,
        key=lambda label: (-safe_float(summary_by_label[label].get("pass_at_3"), -1), label),
    )
    width = 1320
    row_h = 68
    margin_l, margin_r, margin_t, margin_b = 335, 108, 168, 86
    height = margin_t + row_h * max(len(labels), 1) + margin_b
    plot_w = width - margin_l - margin_r
    bar_h = 26

    body: list[str] = []
    body.append(svg_text(48, 42, "Known Blocker Lifecycle", 28, "#101828", weight="700"))
    body.append(
        svg_text(
            48,
            68,
            "One row is a known blocker in an evaluated pass; labels are deterministic matching proxies.",
            15,
            "#667085",
        )
    )

    legend_x = 48
    legend_y = 108
    for idx, stage in enumerate(BLOCKER_LIFECYCLE_ORDER):
        label = BLOCKER_LIFECYCLE_LABELS[stage]
        color = BLOCKER_LIFECYCLE_COLORS[stage]
        item_w = max(150, 7.2 * len(label) + 38)
        if idx == 3:
            legend_x = 48
            legend_y += 26
        body.append(f'<rect x="{legend_x}" y="{legend_y - 11}" width="13" height="13" fill="{color}" stroke="#d0d5dd" stroke-width="0.5"/>')
        body.append(svg_text(legend_x + 18, legend_y, label, 12, "#344054"))
        legend_x += item_w

    body.append(svg_text(width - 20, margin_t - 52, "pass@3", 12, "#344054", "end", "700"))
    for tick in (0, 0.25, 0.5, 0.75, 1.0):
        xx = margin_l + tick * plot_w
        body.append(f'<line class="grid" x1="{xx:.1f}" y1="{margin_t - 15}" x2="{xx:.1f}" y2="{height - margin_b + 10}"/>')
        body.append(svg_text(xx, margin_t - 25, f"{int(tick * 100)}%", 11, "#667085", "middle"))

    if not labels:
        body.append(svg_text(width / 2, height / 2, "No matched blocker registry rows found.", 15, "#667085", "middle"))
        svg(FIG_DIR / "11_blocker_lifecycle_proxy.svg", width, height, "\n".join(body))
        return

    for idx, label in enumerate(labels):
        y = margin_t + idx * row_h
        summary = summary_by_label[label]
        stats = group_stats.get(label, {})
        group_total = safe_int(stats.get("blocker_rows"))
        question_share = safe_float(stats.get("question_targeted_share"), math.nan)
        answer_share = safe_float(stats.get("answer_received_share"), math.nan)
        body.append(svg_text(48, y + 4, compact_group_name(summary), 12, "#101828", weight="700"))
        detail = f"n={group_total} blocker-pass rows"
        if not math.isnan(question_share) and not math.isnan(answer_share):
            detail += f" | targeted {pct(question_share)} | answer matched {pct(answer_share)}"
        body.append(svg_text(48, y + 23, detail, 11, "#667085"))

        x0 = margin_l
        yy = y - 14
        for stage in BLOCKER_LIFECYCLE_ORDER:
            stage_row = by_group_stage[label].get(stage)
            share = clamp01(safe_float(stage_row.get("share"), 0.0)) if stage_row else 0.0
            w = share * plot_w
            if w <= 0.15:
                continue
            color = BLOCKER_LIFECYCLE_COLORS[stage]
            body.append(f'<rect x="{x0:.1f}" y="{yy:.1f}" width="{w:.1f}" height="{bar_h}" fill="{color}"/>')
            if x0 > margin_l and w > 2:
                body.append(f'<line x1="{x0:.1f}" y1="{yy:.1f}" x2="{x0:.1f}" y2="{yy + bar_h:.1f}" stroke="#fbfbf8" stroke-width="1.2"/>')
            if share >= 0.08:
                body.append(
                    svg_text(
                        x0 + w / 2,
                        yy + 17,
                        f"{int(round(share * 100))}%",
                        11,
                        text_fill_for_background(color),
                        "middle",
                        "700",
                    )
                )
            x0 += w

        pass3 = safe_float(summary.get("pass_at_3"), math.nan)
        body.append(svg_text(width - margin_r + 10, y + 4, pct(pass3), 12, "#101828", weight="700"))

    svg(FIG_DIR / "11_blocker_lifecycle_proxy.svg", width, height, "\n".join(body))


def plot_terminal_mix(mix_rows: list[dict[str, Any]], summaries: list[dict[str, Any]]) -> None:
    summary_by_label = {row["group_label"]: row for row in summaries if row["condition"] == "ask_human" and plot_ready(row)}
    groups = sorted(
        {row["group_label"] for row in mix_rows if row["group_label"] in summary_by_label},
        key=lambda label: safe_float(summary_by_label.get(label, {}).get("pass_at_3"), -1),
        reverse=True,
    )
    states = [state for state in TERMINAL_LABELS if any(row["terminal_evidence_state"] == state for row in mix_rows)]
    width = 1280
    row_h = 62
    margin_l, margin_r, margin_t, margin_b = 340, 90, 124, 132
    height = margin_t + row_h * max(len(groups), 1) + margin_b
    plot_w = width - margin_l - margin_r
    by_group_state = {(row["group_label"], row["terminal_evidence_state"]): row for row in mix_rows}
    totals = {label: sum(by_group_state.get((label, state), {}).get("count", 0) for state in states) for label in groups}

    body = []
    body.append(svg_text(48, 42, "Terminal Evidence Mix", 28, "#101828", weight="700"))
    body.append(svg_text(48, 68, "Deterministic labels over failed AskHuman pass-level trajectories; no LLM judge required.", 15, "#667085"))
    body.append(svg_text(margin_l, margin_t - 24, "Share of failed AskHuman passes", 12, "#344054", weight="700"))
    body.append(svg_text(width - margin_r, margin_t - 24, "failed passes", 12, "#667085", "end"))
    for tick in [0, 0.25, 0.5, 0.75, 1.0]:
        xx = margin_l + tick * plot_w
        body.append(f'<line class="grid" x1="{xx:.1f}" y1="{margin_t-12}" x2="{xx:.1f}" y2="{height-margin_b}"/>')
        body.append(svg_text(xx, height - margin_b + 28, f"{int(tick * 100)}%", 12, "#667085", "middle"))
    for idx, label in enumerate(groups):
        yy = margin_t + idx * row_h
        model, scaffold = (label.split(" / ", 1) + [""])[:2] if " / " in label else (label, "")
        body.append(svg_text(48, yy + 14, model, 12, "#101828", weight="700"))
        body.append(svg_text(48, yy + 32, scaffold, 11, "#667085"))
        x0 = margin_l
        total = totals[label]
        for state in states:
            count = by_group_state.get((label, state), {}).get("count", 0)
            share = count / total if total else 0
            w = share * plot_w
            if w <= 0:
                continue
            color = TERMINAL_COLORS.get(state, "#adb5bd")
            body.append(f'<rect x="{x0:.1f}" y="{yy:.1f}" width="{w:.1f}" height="34" fill="{color}"/>')
            if x0 > margin_l:
                body.append(f'<line x1="{x0:.1f}" y1="{yy:.1f}" x2="{x0:.1f}" y2="{yy + 34:.1f}" stroke="#fbfbf8" stroke-width="1"/>')
            if share >= 0.095:
                body.append(svg_text(x0 + w / 2, yy + 22, f"{int(round(share * 100))}%", 11, text_fill_for_background(color), "middle", "700"))
            x0 += w
        pass3 = safe_float(summary_by_label.get(label, {}).get("pass_at_3"), math.nan)
        body.append(svg_text(width - margin_r + 12, yy + 13, f"n={total}", 11, "#667085"))
        body.append(svg_text(width - margin_r + 12, yy + 31, f"pass@3 {pct(pass3)}", 10, "#98a2b3"))
    body.append(svg_text(margin_l + plot_w / 2, height - margin_b + 58, "terminal evidence bucket share", 12, "#344054", "middle", "700"))

    legend_y = height - 42
    legend_x = 48
    legend_step = 206
    for state in states:
        color = TERMINAL_COLORS.get(state, "#adb5bd")
        body.append(f'<rect x="{legend_x}" y="{legend_y-11}" width="13" height="13" rx="2" fill="{color}"/>')
        body.append(svg_text(legend_x + 18, legend_y, TERMINAL_LABELS.get(state, state), 12, "#344054"))
        legend_x += legend_step
        if legend_x > width - 210:
            legend_x = 48
            legend_y += 24

    svg(FIG_DIR / "04_terminal_evidence_mix.svg", width, height, "\n".join(body))


def plot_strategy_buckets(bucket_rows: list[dict[str, Any]], summaries: list[dict[str, Any]]) -> None:
    summary_by_label = {row["group_label"]: row for row in summaries if row["condition"] == "ask_human" and plot_ready(row)}
    ready_labels = set(summary_by_label)
    groups = sorted(
        {row["group_label"] for row in bucket_rows if row["group_label"] in ready_labels},
        key=lambda label: safe_float(summary_by_label[label].get("pass_at_3"), -1),
        reverse=True,
    )
    buckets = [
        "explored then asked before write",
        "upfront ask before read",
        "wrote before first ask",
        "no ask",
        "ask logged, no ask action",
        "other mixed strategy",
    ]
    display_labels = {
        "explored then asked before write": "explore -> ask -> write",
        "upfront ask before read": "ask before read",
        "wrote before first ask": "write before ask",
        "no ask": "no ask",
        "ask logged, no ask action": "logged ask, no parsed action",
        "other mixed strategy": "other mixed",
    }
    colors = {
        "explored then asked before write": "#009e73",
        "upfront ask before read": "#e69f00",
        "wrote before first ask": "#d55e00",
        "no ask": "#4b5563",
        "ask logged, no ask action": "#0072b2",
        "other mixed strategy": "#cc79a7",
    }
    by_group_bucket = {(row["group_label"], row["strategy_bucket"]): row for row in bucket_rows}
    totals = {
        label: sum(safe_int(by_group_bucket.get((label, bucket), {}).get("count")) for bucket in buckets)
        for label in groups
    }
    width = 1180
    row_gap = 54
    bar_h = 24
    margin_l, margin_r, margin_t = 330, 92, 122
    legend_top = margin_t + row_gap * max(len(groups), 1) + 30
    height = legend_top + 68
    plot_w = width - margin_l - margin_r
    body = []
    body.append(svg_text(48, 42, "Trajectory Strategy Fingerprints", 28, "#101828", weight="700"))
    body.append(svg_text(48, 68, "Pass-level trajectories grouped by first-help pattern on underspecified SWE tasks.", 15, "#667085"))
    grid_top = margin_t - 18
    grid_bottom = margin_t + row_gap * max(len(groups) - 1, 0) + bar_h + 18
    for tick in [0, 0.25, 0.5, 0.75, 1.0]:
        xx = margin_l + tick * plot_w
        body.append(f'<line class="grid" x1="{xx:.1f}" y1="{grid_top:.1f}" x2="{xx:.1f}" y2="{grid_bottom:.1f}"/>')
        body.append(svg_text(xx, margin_t - 28, f"{int(tick * 100)}%", 12, "#667085", "middle"))
    body.append(f'<line class="axis" x1="{margin_l}" y1="{grid_bottom:.1f}" x2="{margin_l + plot_w}" y2="{grid_bottom:.1f}"/>')
    for idx, label in enumerate(groups):
        yy = margin_t + idx * row_gap
        body.append(f'<line x1="48" y1="{yy + bar_h + 14:.1f}" x2="{width - 48}" y2="{yy + bar_h + 14:.1f}" stroke="#eef0f2" stroke-width="1"/>')
        body.append(svg_text(48, yy + 17, label, 13, "#101828", weight="700"))
        body.append(svg_text(width - 48, yy + 17, f"n={totals[label]}", 12, "#667085", "end"))
        body.append(
            f'<rect x="{margin_l}" y="{yy}" width="{plot_w}" height="{bar_h}" fill="#eef0f2" stroke="#d0d5dd" stroke-width="1"/>'
        )
        x0 = margin_l
        for bucket in buckets:
            share = safe_float(by_group_bucket.get((label, bucket), {}).get("share_within_group"), 0.0)
            w = share * plot_w
            if w <= 0:
                continue
            color = colors[bucket]
            body.append(f'<rect x="{x0:.1f}" y="{yy:.1f}" width="{w:.1f}" height="{bar_h}" fill="{color}"/>')
            if share >= 0.11:
                body.append(svg_text(x0 + w / 2, yy + 16, f"{int(round(share * 100))}%", 11, text_fill_for_background(color), "middle", "700"))
            if x0 > margin_l and w > 2:
                body.append(f'<line x1="{x0:.1f}" y1="{yy:.1f}" x2="{x0:.1f}" y2="{yy + bar_h:.1f}" stroke="#fbfbf8" stroke-width="1.2"/>')
            x0 += w
    legend_y = legend_top + 4
    legend_x = 48
    for bucket in buckets:
        label = display_labels[bucket]
        item_w = max(116, 8.0 * len(label) + 34)
        if legend_x + item_w > width - 48:
            legend_x = 48
            legend_y += 24
        body.append(f'<rect x="{legend_x}" y="{legend_y-11}" width="13" height="13" fill="{colors[bucket]}"/>')
        body.append(svg_text(legend_x + 18, legend_y, label, 12, "#344054"))
        legend_x += item_w
    svg(FIG_DIR / "05_strategy_buckets.svg", width, height, "\n".join(body))


def plot_trajectory_action_phenotype_families(
    action_rows: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    include_open: bool = False,
    include_idle_end: bool = False,
    file_suffix: str | None = None,
) -> list[Path]:
    included_families = [family for family in MODEL_FAMILY_ORDER if include_open or family in DEFAULT_PHENOTYPE_FAMILIES]
    summary_by_label = {row["group_label"]: row for row in summaries if row["condition"] == "ask_human" and plot_ready(row)}
    rows_by_family_label_turn: dict[str, dict[str, dict[int, dict[str, Any]]]] = defaultdict(lambda: defaultdict(dict))
    for row in action_rows:
        family = str(row.get("family") or "")
        label = str(row.get("group_label") or "")
        turn = safe_int(row.get("turn_index"))
        if family and label and turn:
            rows_by_family_label_turn[family][label][turn] = row

    paths: list[Path] = []
    for family in included_families:
        labels = sorted(
            rows_by_family_label_turn.get(family, {}),
            key=lambda label: phenotype_group_sort_key(label, summary_by_label),
        )
        if not labels:
            continue
        max_turns = max(max(turns) for turns in rows_by_family_label_turn[family].values() if turns)
        width = 1240
        panel_h = 104
        row_gap = 146
        margin_l, margin_r, margin_t = 255, 64, 142
        legend_h = 58
        height = margin_t + row_gap * len(labels) + legend_h
        plot_w = width - margin_l - margin_r
        bar_w = plot_w / max_turns
        legend_order = ACTION_PHENOTYPE_WITH_END_LEGEND_ORDER if include_idle_end else ACTION_PHENOTYPE_LEGEND_ORDER
        stack_order = ACTION_PHENOTYPE_WITH_END_STACK_ORDER if include_idle_end else ACTION_PHENOTYPE_STACK_ORDER
        subtitle = (
            "Action share by turn; thought-only no-tool turns are separate from the idle/end top cap."
            if include_idle_end
            else "Action share by turn among active trajectories; thought-only no-tool turns are shown explicitly."
        )

        body: list[str] = []
        body.append(svg_text(48, 42, f"{family} Trajectory Action Phenotypes", 28, "#101828", weight="700"))
        body.append(svg_text(48, 68, subtitle, 15, "#667085"))

        legend_x = 48
        legend_y = 106
        for action in legend_order:
            color = ACTION_PHENOTYPE_COLORS[action]
            body.append(f'<rect x="{legend_x}" y="{legend_y - 11}" width="13" height="13" fill="{color}"/>')
            body.append(svg_text(legend_x + 18, legend_y, ACTION_PHENOTYPE_LABELS[action], 12, "#344054"))
            legend_x += max(92, 7.5 * len(ACTION_PHENOTYPE_LABELS[action]) + 38)

        for idx, label in enumerate(labels):
            summary = summary_by_label.get(label, {})
            y0 = margin_t + idx * row_gap
            y_base = y0 + panel_h
            show_x_labels = idx == len(labels) - 1
            body.append(svg_text(48, y0 + 17, label, 13, "#101828", weight="700"))
            body.append(svg_text(48, y0 + 36, f"pass@3 {pct(safe_float(summary.get('pass_at_3')))}", 12, "#667085"))
            trajectory_n = safe_int(rows_by_family_label_turn[family][label].get(1, {}).get("trajectories"))
            body.append(svg_text(48, y0 + 54, f"n={trajectory_n}", 12, "#667085"))
            body.append(f'<rect x="{margin_l}" y="{y0}" width="{plot_w}" height="{panel_h}" fill="#ffffff" stroke="#d0d5dd" stroke-width="1"/>')

            for frac in (0.25, 0.5, 0.75):
                yy = y_base - frac * panel_h
                body.append(f'<line class="grid" x1="{margin_l}" y1="{yy:.1f}" x2="{margin_l + plot_w}" y2="{yy:.1f}"/>')
            for tick in range(0, max_turns + 1, 20):
                xx = margin_l + tick / max_turns * plot_w
                body.append(f'<line class="grid" x1="{xx:.1f}" y1="{y0}" x2="{xx:.1f}" y2="{y_base}"/>')
                if show_x_labels:
                    body.append(svg_text(xx, y_base + 18, str(tick), 11, "#667085", "middle"))

            for turn in range(1, max_turns + 1):
                turn_row = rows_by_family_label_turn[family][label].get(turn, {})
                x0 = margin_l + (turn - 1) * bar_w
                stack_y = y_base
                for action in stack_order:
                    share = safe_float(turn_row.get(f"{action}_share"), 0.0)
                    if math.isnan(share):
                        continue
                    h = share * panel_h
                    if h <= 0.1:
                        continue
                    stack_y -= h
                    body.append(
                        f'<rect x="{x0:.2f}" y="{stack_y:.2f}" width="{bar_w + 0.15:.2f}" height="{h:.2f}" fill="{ACTION_PHENOTYPE_COLORS[action]}"/>'
                    )

            body.append(f'<line class="axis" x1="{margin_l}" y1="{y_base}" x2="{margin_l + plot_w}" y2="{y_base}"/>')
            if show_x_labels:
                body.append(svg_text(margin_l + plot_w / 2, y_base + 38, "turn index", 12, "#344054", "middle", "600"))
            body.append(svg_text(margin_l - 16, y0 + 5, "100%", 11, "#667085", "end"))
            body.append(svg_text(margin_l - 16, y_base + 4, "0%", 11, "#667085", "end"))

        file_family = family.lower().replace(" ", "_")
        suffix = file_suffix if file_suffix is not None else ("_with_end" if include_idle_end else "")
        path = FIG_DIR / f"06_action_phenotypes_{file_family}{suffix}.svg"
        svg(path, width, height, "\n".join(body))
        paths.append(path)
    return paths


def write_release_md(
    verification: dict[str, Any],
    summaries: list[dict[str, Any]],
    full_info_dirs: list[str],
) -> None:
    rows = [row for row in summaries if row["condition"] == "ask_human"]
    full_info_rows = [row for row in summaries if row["condition"] == "full_info"]
    lines: list[str] = []
    lines.append("# Trust Horizon Release Asset Pass")
    lines.append("")
    lines.append("## Path Verification")
    lines.append("")
    lines.append("- Native release roots are read from `--native-runs-root`; the script looks for `*_swe_skill3` plus explicitly listed custom-tool and FullInfo run directories.")
    for item in verification["native_skill3"]:
        note = f" ({item['note']})" if item.get("note") else ""
        lines.append(
            f"  - `{Path(item['root']).name if item['root'] else 'not configured'}`: {item['pass_dirs']} pass dirs, "
            f"summary={item['has_summary_json']}, pass_level={item['has_pass_level_json']}.{note}"
        )
    lines.append("- SWE-agent raw rows are read from `--swe-agent-raw-root` when configured.")
    for item in verification["swe_agent_raw"]:
        note = f" ({item['note']})" if item.get("note") else ""
        lines.append(
            f"  - `{Path(item['root']).name if item['root'] else 'not configured'}`: {item['metrics_files']} metrics files, "
            f"{item['trajectory_files']} trajectories, dataset_metrics={item['has_dataset_metrics_json']}.{note}"
        )
    for item in verification.get("swe_agent_analysis_csv", []):
        note = f" ({item['note']})" if item.get("note") else ""
        lines.append(
            f"- SWE-agent analysis-only supplement `{Path(item['root']).name if item['root'] else 'not configured'}`: "
            f"{item['rows_ingested']} Gemini rows ingested from derived CSVs; "
            f"turn/action CSVs available={item['turn_actions_csv'] and item['first_ask_relevance_csv']}.{note}"
        )
    lines.append("")
    lines.append("## Current Caveats")
    lines.append("")
    lines.append("- Matched native FullInfo roots are now included for ADK, Claude Code, Codex, and OpenCode. SWE-agent FullInfo is still not included in this release bundle.")
    if full_info_dirs:
        lines.append("- Older per-task native FullInfo directories also exist outside the release roots; they remain ignored for headline plots:")
        for path in full_info_dirs[:6]:
            lines.append(f"  - `{path}`")
        if len(full_info_dirs) > 6:
            lines.append(f"  - ... plus {len(full_info_dirs) - 6} more")
    lines.append("- `opencode_swe_skill3` now has a `metrics/pass_level.json` index and is included in the standard native-root ingestion.")
    low_coverage_rows = [row for row in rows if safe_float(row.get("eval_coverage"), 0.0) < MIN_EVAL_COVERAGE_FOR_FIGURES]
    if low_coverage_rows:
        low_labels = ", ".join(f"{row['group_label']} ({pct(safe_float(row['eval_coverage']))})" for row in low_coverage_rows)
        lines.append(f"- Headline figures exclude groups below {int(MIN_EVAL_COVERAGE_FOR_FIGURES * 100)}% eval coverage: {low_labels}.")
    else:
        lines.append(f"- All AskHuman groups currently meet the {int(MIN_EVAL_COVERAGE_FOR_FIGURES * 100)}% eval-coverage threshold for headline figures.")
    lines.append("- SWE-agent Gemini is currently analysis-only: it appears in pass/ask/strategy comparisons, but is excluded from terminal failure mix until raw metrics/trajectory directories are added beside the other SWE-agent models.")
    lines.append("- Answer incorporation is not plotted here; that needs the LLM integration judge output. Terminal evidence and strategy buckets are deterministic.")
    lines.append("- Terminal failure mix now splits `patch made / no submit` from true `no patch/no submit`, and `timeout after patch` from `timeout before patch`; native harnesses had many patch-producing runs previously collapsed into broader buckets.")
    lines.append("- Point marks use shape for model family: Claude star, GPT diamond, Gemini triangle, GLM square, and hue for pass@3. Point size is not metric-encoded.")
    lines.append("- `ask logged, no ask action` means blocker/question metadata records questions, but the action trace has no parsed `ASK` action; this is a parser/format audit bucket, not a model strategy.")
    lines.append("- `data/question_blocker_integration_proxy.csv` adds deterministic follow-through proxies for question/blocker integration. It is not a semantic answer-incorporation judge.")
    lines.append("- `data/ask_timing_by_group.csv` breaks pass rows into ask-before-edit, ask-after-edit, ask-without-edit, parser-audit, and no-ask buckets.")
    lines.append("- `data/blocker_lifecycle_proxy.csv` is blocker-centered: one row per known blocker per evaluated pass, with deterministic stages for detection, targeted asking, matched answers, follow-up, and solved runs. It skips analysis-only rows without raw trajectories or registries.")
    lines.append("- Action-phenotype panels use `Idle/end` as a top cap in the main files; active-normalized context copies are emitted for readers familiar with the earlier turn-normalized plots.")
    lines.append("- `Thought only` means a logged trajectory turn with a non-empty thought but no parsed tool action; it is separated from the post-trajectory `Idle/end` cap.")
    lines.append("- `data/native_other_no_action_audit.csv` audits Native Codex/Claude Code `OTHER` and raw `NO_ACTION` buckets. The release classifier now unwraps common harness action wrappers, so Codex `Edit:` calls count as `WRITE`, wrapped shell snippets can count as `EXECUTE`/`READ`/`TEST`, and raw thought-only empty actions count as `THOUGHT_ONLY`.")
    lines.append("- `data/thought_only_no_tool_audit.csv` breaks those raw thought-only turns into coarse intent buckets for inspection.")
    lines.append("")
    lines.append("## Generated Figures")
    lines.append("")
    lines.append("- `figures/01_same_model_different_scaffold.svg`: matched native FullInfo pass@3 vs AskHuman pass@3, showing the context drop-off below the diagonal.")
    lines.append("- `figures/02_detection_targeting.svg`: Zi-style uncertainty detection vs asking-the-right-question plot.")
    lines.append("- `figures/03_pass_vs_ask_burden.svg`: pass@3 against ask precision, with asks/pass kept as point annotation.")
    lines.append("- `figures/04_terminal_evidence_mix.svg`: deterministic terminal failure anatomy.")
    lines.append("- `figures/05_strategy_buckets.svg`: side piece on scaffold trajectory style.")
    lines.append("- `figures/06_action_phenotypes_{gpt,claude,gemini}.svg`: per-family trajectory action phenotypes with `Idle/end` stacked on top.")
    lines.append("- `figures/06_action_phenotypes_{gpt,claude,gemini}_active_normalized.svg`: context version normalized over still-active trajectories, matching the earlier plot style.")
    lines.append("- `figures/07_ask_funnel.svg`: side-by-side blocker recall and ask precision bars.")
    lines.append("- `figures/08_first_ask_timing.svg`: first-ask timing relative to the first edit.")
    lines.append("- `figures/09_question_follow_through.svg`: deterministic proxies for using information after asking.")
    lines.append("- `figures/10_same_model_harness_failure_profile.svg`: same-model Native vs SWE-agent profiles for pass@3, precision, and recall.")
    lines.append("- `figures/11_blocker_lifecycle_proxy.svg`: known-blocker lifecycle stack showing detection, targeted asking, answer matching, follow-up, and solved-run endpoints.")
    lines.append("- `figures/12_full_info_gap.svg`: matched native FullInfo vs AskHuman pass@3 ceiling gap.")
    lines.append("")
    lines.append("## AskHuman Summary")
    lines.append("")
    lines.append("| group | pass@3 | precision | recall | asks/pass | rows | eval rows | eval coverage |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for row in sorted(rows, key=lambda row: safe_float(row["pass_at_3"], -1), reverse=True):
        lines.append(
            f"| {row['group_label']} | {pct(safe_float(row['pass_at_3']))} | "
            f"{pct(safe_float(row['ask_precision']))} | {pct(safe_float(row['ask_recall']))} | "
            f"{safe_float(row['avg_questions_per_pass'], 0):.2f} | {row['num_pass_rows']} | {row['num_eval_known_rows']} | "
            f"{pct(safe_float(row['eval_coverage']))} |"
        )
    if full_info_rows:
        lines.append("")
        lines.append("## FullInfo Summary")
        lines.append("")
        lines.append("| group | pass@3 | rows | eval rows | eval coverage |")
        lines.append("|---|---:|---:|---:|---:|")
        for row in sorted(full_info_rows, key=lambda row: safe_float(row["pass_at_3"], -1), reverse=True):
            lines.append(
                f"| {row['group_label']} | {pct(safe_float(row['pass_at_3']))} | "
                f"{row['num_pass_rows']} | {row['num_eval_known_rows']} | {pct(safe_float(row['eval_coverage']))} |"
            )
    lines.append("")
    lines.append("## Suggested Twitter/MD Ordering")
    lines.append("")
    lines.append("1. Lead with AskHuman vs FullInfo pass@3 to show the ceiling implied by missing context.")
    lines.append("2. Follow with detection vs targeting: uncertainty detection is not the same as asking well.")
    lines.append("3. Use pass@3 vs ask precision to connect performance with question targeting quality; use asks/pass as secondary annotation.")
    lines.append("4. Use same-model scaffold comparisons to show that harness choices still matter inside the AskHuman setting.")
    lines.append("5. Use the blocker lifecycle proxy as the first candidate for an answer-integration/exploration-quality diagnostic; validate the semantic claims with judge labels before making it a headline.")
    lines.append("6. Put terminal evidence mix in the MD or as a follow-up tweet; it is solid because it is deterministic, but it is a diagnostic panel rather than the hook.")
    (OUT_DIR / "release_asset_notes.md").write_text(redact_sensitive_text("\n".join(lines)) + "\n")


def find_full_info_dirs() -> list[str]:
    if HARNESS_ROOT is None or not HARNESS_ROOT.exists():
        return []
    roots = []
    for run_name in ("codex_swe", "claude_swe", "adk_swe", "opencode_swe"):
        run_dir = HARNESS_ROOT / run_name
        if run_dir.exists():
            roots.extend(str(path) for path in sorted(run_dir.glob("*/full_info")))
    roots.extend(str(path) for path in sorted(HARNESS_ROOT.glob("fullinfo-e2e-*/*/full_info")))
    return roots


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate Trust Horizon analysis CSVs and SVG figures from local run artifacts.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=OUT_DIR,
        help="Directory where data/, figures/, and release_asset_notes.md should be written.",
    )
    parser.add_argument(
        "--native-runs-root",
        type=Path,
        help="Root containing native harness run directories such as *_swe_skill3 and *_swe_full_info.",
    )
    parser.add_argument(
        "--swe-agent-raw-root",
        type=Path,
        help="Root containing SWE-agent raw per-model trajectory directories.",
    )
    parser.add_argument(
        "--swe-agent-analysis-root",
        type=Path,
        help="Root containing derived SWE-agent analysis CSVs. Defaults to ../analysis/figure10_model_families relative to --swe-agent-raw-root.",
    )
    parser.add_argument(
        "--harbor-root",
        type=Path,
        action="append",
        default=[],
        help="Harbor SWE task root containing swe_*/shared/metadata.json and blocker registries. May be repeated.",
    )
    parser.add_argument(
        "--scrub-local-paths",
        action="store_true",
        help="Redact absolute local paths in generated CSV and JSON metadata outputs.",
    )
    return parser.parse_args()


def configure_paths(args: argparse.Namespace) -> None:
    global OUT_DIR, DATA_DIR, FIG_DIR
    global HARNESS_ROOT, SWE_AGENT_RAW_ROOT, SWE_AGENT_ANALYSIS_ROOT, HIL_BENCH_HARBOR_ROOTS
    global SCRUB_LOCAL_PATHS

    OUT_DIR = args.out_dir.expanduser().resolve()
    DATA_DIR = OUT_DIR / "data"
    FIG_DIR = OUT_DIR / "figures"
    HARNESS_ROOT = args.native_runs_root.expanduser().resolve() if args.native_runs_root else None
    SWE_AGENT_RAW_ROOT = args.swe_agent_raw_root.expanduser().resolve() if args.swe_agent_raw_root else None
    if args.swe_agent_analysis_root:
        SWE_AGENT_ANALYSIS_ROOT = args.swe_agent_analysis_root.expanduser().resolve()
    elif SWE_AGENT_RAW_ROOT is not None:
        SWE_AGENT_ANALYSIS_ROOT = (SWE_AGENT_RAW_ROOT.parent / "analysis" / "figure10_model_families").resolve()
    else:
        SWE_AGENT_ANALYSIS_ROOT = None
    HIL_BENCH_HARBOR_ROOTS = [path.expanduser().resolve() for path in args.harbor_root]
    SCRUB_LOCAL_PATHS = bool(getattr(args, "scrub_local_paths", False))


def main() -> None:
    configure_paths(parse_args())
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    native_rows, native_verification = ingest_native_skill3()
    swe_rows, swe_verification = ingest_swe_agent_raw()
    swe_analysis_rows, swe_analysis_verification = ingest_swe_agent_analysis_only()
    rows = native_rows + swe_rows + swe_analysis_rows
    verification = {
        "native_skill3": native_verification,
        "swe_agent_raw": swe_verification,
        "swe_agent_analysis_csv": swe_analysis_verification,
        "full_info_dirs_found_outside_requested_roots": find_full_info_dirs(),
    }
    verification_output = scrub_for_output(verification)
    if not rows:
        write_csv(DATA_DIR / "per_run_features.csv", [])
        write_csv(DATA_DIR / "summary_by_group.csv", [])
        (DATA_DIR / "path_verification.json").write_text(json.dumps(verification_output, indent=2, sort_keys=True) + "\n")
        write_release_md(verification_output, [], verification_output["full_info_dirs_found_outside_requested_roots"])
        print("No input rows found. Check --native-runs-root, --swe-agent-raw-root, and --swe-agent-analysis-root.")
        print(f"Assets: {OUT_DIR}")
        return

    summaries = summarize_groups(rows)
    full_gap = full_info_gap_rows(summaries, rows)
    mix = terminal_mix(rows)
    buckets = strategy_buckets(rows)
    action_audit = native_other_no_action_audit(rows)
    thought_only_audit = thought_only_no_tool_audit(rows)
    action_phenotypes_active = trajectory_action_phenotypes_by_turn(rows, summaries)
    action_phenotypes_with_end = trajectory_action_phenotypes_by_turn(rows, summaries, include_idle_end=True)
    integration = question_blocker_integration(rows)
    timing = ask_timing_by_group(rows, summaries)
    blocker_lifecycle_rows, blocker_lifecycle_summary = blocker_lifecycle_proxy(rows)

    write_csv(DATA_DIR / "per_run_features.csv", [scrub_row_for_output(row) for row in rows])
    write_csv(DATA_DIR / "summary_by_group.csv", summaries)
    write_csv(DATA_DIR / "full_info_gap.csv", full_gap)
    write_csv(DATA_DIR / "terminal_evidence_mix.csv", mix)
    write_csv(DATA_DIR / "strategy_buckets.csv", buckets)
    write_csv(DATA_DIR / "native_other_no_action_audit.csv", action_audit)
    write_csv(DATA_DIR / "thought_only_no_tool_audit.csv", thought_only_audit)
    write_csv(DATA_DIR / "trajectory_action_phenotypes_by_turn.csv", action_phenotypes_with_end)
    write_csv(DATA_DIR / "trajectory_action_phenotypes_by_turn_with_end.csv", action_phenotypes_with_end)
    write_csv(DATA_DIR / "trajectory_action_phenotypes_by_turn_active_normalized.csv", action_phenotypes_active)
    write_csv(DATA_DIR / "question_blocker_integration_proxy.csv", integration)
    write_csv(DATA_DIR / "ask_timing_by_group.csv", timing)
    write_csv(DATA_DIR / "blocker_lifecycle_proxy.csv", blocker_lifecycle_rows)
    write_csv(DATA_DIR / "blocker_lifecycle_summary.csv", blocker_lifecycle_summary)
    (DATA_DIR / "path_verification.json").write_text(json.dumps(verification_output, indent=2, sort_keys=True) + "\n")

    plot_same_model_dumbbell(summaries, full_gap)
    plot_full_info_gap(full_gap)
    plot_detection_targeting(summaries)
    plot_pass_vs_ask_burden(summaries)
    plot_ask_funnel(summaries)
    plot_first_ask_timing(timing, summaries)
    plot_question_follow_through(integration, summaries)
    plot_same_model_harness_failure_profile(summaries)
    plot_blocker_lifecycle_proxy(blocker_lifecycle_summary, summaries)
    plot_terminal_mix(mix, summaries)
    plot_strategy_buckets(buckets, summaries)
    plot_trajectory_action_phenotype_families(action_phenotypes_with_end, summaries, include_idle_end=True, file_suffix="")
    plot_trajectory_action_phenotype_families(action_phenotypes_active, summaries, file_suffix="_active_normalized")
    write_release_md(verification_output, summaries, verification_output["full_info_dirs_found_outside_requested_roots"])

    print(f"Wrote {len(rows)} per-pass rows")
    print(f"Wrote {len(summaries)} summary rows")
    print(f"Assets: {OUT_DIR}")


if __name__ == "__main__":
    main()
