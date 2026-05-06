#!/usr/bin/env python3
"""Validate normalized Trust Horizon trajectory JSONL files."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVALS = ROOT / "evals"

REQUIRED_FIELDS = {
    "type",
    "timestamp",
    "run_id",
    "instance_id",
    "harness",
    "attempt_index",
    "event_index",
    "event_type",
    "native_event_type",
    "native_payload",
    "normalized_request_type",
    "question",
    "answer",
    "ask_human_status",
    "matched_blocker_ids",
    "matched_source_ids",
    "approval_decision",
    "approval_grounding",
    "files_changed",
    "commands_run",
    "tests_run",
    "patch_path",
    "final_status",
    "audit",
}

LIST_FIELDS = {"matched_blocker_ids", "matched_source_ids", "files_changed", "commands_run", "tests_run"}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            if not isinstance(parsed, dict):
                raise ValueError(f"{path}:{line_number}: trajectory event must be an object")
            rows.append(parsed)
    return rows


def validate_file(path: Path) -> list[str]:
    errors: list[str] = []
    rows = load_jsonl(path)
    if not rows:
        return [f"{path}: empty trajectory"]

    seen_indexes: list[int] = []
    human_requests: dict[str, dict[str, Any]] = {}
    human_results: set[str] = set()
    saw_submission = False
    saw_attempt_end = False

    for line_number, event in enumerate(rows, start=1):
        missing = sorted(REQUIRED_FIELDS - event.keys())
        if missing:
            errors.append(f"{path}:{line_number}: missing fields: {', '.join(missing)}")
        for field in LIST_FIELDS:
            if field in event and not isinstance(event[field], list):
                errors.append(f"{path}:{line_number}: {field} must be a list")
        if "audit" in event and not isinstance(event["audit"], dict):
            errors.append(f"{path}:{line_number}: audit must be an object")

        event_index = event.get("event_index")
        if not isinstance(event_index, int):
            errors.append(f"{path}:{line_number}: event_index must be an integer")
        else:
            seen_indexes.append(event_index)

        event_type = str(event.get("type") or "")
        request_id = ((event.get("tool_args") or {}) if isinstance(event.get("tool_args"), dict) else {}).get("request_id")
        request_id = event.get("request_id") or request_id
        if event_type == "human_input_normalized_event":
            if not request_id:
                errors.append(f"{path}:{line_number}: human_input_normalized_event missing request_id")
            else:
                human_requests[str(request_id)] = event
        elif event_type == "human_input_result":
            if not request_id:
                errors.append(f"{path}:{line_number}: human_input_result missing request_id")
            else:
                human_results.add(str(request_id))
        elif event_type == "submission":
            saw_submission = True
        elif event_type == "attempt_end":
            saw_attempt_end = True

    if seen_indexes != list(range(len(rows))):
        errors.append(f"{path}: event_index values must be contiguous from 0")

    missing_results = sorted(request_id for request_id, event in human_requests.items() if event.get("normalized_request_type") in {"clarification", "elicitation"} and request_id not in human_results)
    if missing_results:
        errors.append(f"{path}: human clarification requests missing results: {', '.join(missing_results)}")

    if not saw_submission:
        errors.append(f"{path}: missing submission event")
    if not saw_attempt_end:
        errors.append(f"{path}: missing attempt_end event")
    return errors


def trajectory_files(run_dir: Path) -> list[Path]:
    return sorted((run_dir / "trajectories").glob("*/*/attempt-*/trajectory.jsonl"))


def validate_run(run_dir: Path) -> dict[str, Any]:
    files = trajectory_files(run_dir)
    errors: list[str] = []
    for path in files:
        errors.extend(validate_file(path))
    return {
        "run_dir": str(run_dir),
        "trajectory_files": len(files),
        "valid": not errors and bool(files),
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", help="Run id under evals/")
    parser.add_argument("--run-dir", type=Path, help="Explicit run directory")
    parser.add_argument("--out", type=Path, help="Optional report path")
    args = parser.parse_args()

    if not args.run_dir and not args.run_id:
        raise SystemExit("Provide --run-id or --run-dir")
    run_dir = args.run_dir or (DEFAULT_EVALS / str(args.run_id))
    report = validate_run(run_dir)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    if not report["valid"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
