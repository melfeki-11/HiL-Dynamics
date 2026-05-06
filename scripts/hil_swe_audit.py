#!/usr/bin/env python3
"""Audit HiL-SWE Claude Code/Codex run trajectories and render the final report."""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
UNKNOWN = "UNKNOWN"


def load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    except FileNotFoundError:
        pass
    return rows


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def fmt(value: Any) -> str:
    if value is None:
        return "missing"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def short_instance(instance_id: str) -> str:
    if "ansible" in instance_id:
        return "ansible"
    if "4817fe" in instance_id:
        return "proton-4817"
    if "708ed4" in instance_id:
        return "proton-708"
    return instance_id[:24]


def prepared_label(prepared_dir: Path) -> str:
    manifest = load_json(prepared_dir / "manifest.json") or {}
    limit = manifest.get("limit")
    if limit:
        return f"First-{limit}"
    name = prepared_dir.name
    if "first" in name:
        return f"First-{name.rsplit('first', 1)[-1]}"
    return "Selected"


def attempt_key_from_path(path: Path, run_dir: Path) -> dict[str, Any]:
    parts = path.relative_to(run_dir).parts
    return {
        "harness": parts[1],
        "instance_id": parts[2],
        "attempt_index": int(parts[3].replace("attempt-", "")),
    }


def patch_files(patch_path: Path) -> list[str]:
    files: list[str] = []
    if not patch_path.exists():
        return files
    for line in patch_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 3:
                files.append(parts[2][2:] if parts[2].startswith("a/") else parts[2])
    return files


def event_text(event: dict[str, Any]) -> str:
    return json.dumps(event, sort_keys=True, default=str)


def is_repo_inspection(text: str) -> bool:
    return bool(re.search(r"\b(git status|git diff|find |rg |grep |sed -n|ls\b|cat )", text))


def is_test_event(text: str) -> bool:
    return bool(re.search(r"\b(pytest|jest|npm test|yarn .*test|compileall|py_compile|eslint)\b", text, re.I))


def is_edit_event(text: str) -> bool:
    return bool(re.search(r"\b(apply_patch|str_replace|file_change|patch\.diff|diff --git|write_file|edit)\b", text, re.I))


def audit_attempt(path: Path, run_dir: Path) -> dict[str, Any]:
    key = attempt_key_from_path(path, run_dir)
    attempt_dir = path.parent
    attempt = load_json(attempt_dir / "attempt.json") or {}
    events = load_jsonl(path)
    request_by_id: dict[str, dict[str, Any]] = {}
    questions: list[dict[str, Any]] = []
    first_repo = first_test = first_edit = first_question = None
    sdk_errors: list[str] = []
    submitted = False
    context_limit = False

    for index, event in enumerate(events):
      text = event_text(event)
      if first_repo is None and is_repo_inspection(text):
          first_repo = index
      if first_test is None and is_test_event(text):
          first_test = index
      if first_edit is None and is_edit_event(text):
          first_edit = index
      if event.get("type") in {"sdk_error", "attempt_error"}:
          sdk_errors.append(str(event.get("error") or text)[:1000])
      if event.get("type") == "submission":
          submitted = True
      if re.search(r"context[-_ ]+(limit|window)|exit_context|timed out|attempt timed out|timeout after|maximum number of turns|token (limit|budget|window)", text, re.I):
          context_limit = True

      if event.get("type") in {"human_input_raw_event", "human_input_normalized_event"}:
          request_id = str(event.get("request_id") or "")
          request = event.get("request") if isinstance(event.get("request"), dict) else {}
          request_type = event.get("request_type") or request.get("request_type")
          if request_id and request_type in {"clarification", "elicitation"}:
              question = event.get("question") or request.get("normalized_question") or ""
              request_by_id[request_id] = {"index": index, "question": question, "request_type": request_type}
              if first_question is None:
                  first_question = index

      if event.get("type") == "human_input_result" and event.get("request_type") in {"clarification", "elicitation"}:
          request_id = str(event.get("request_id") or "")
          request = request_by_id.get(request_id, {})
          result = event.get("result") if isinstance(event.get("result"), dict) else {}
          blocker_id = str(result.get("blocker_id") or UNKNOWN)
          question = str(request.get("question") or "")
          questions.append({
              "event_index": index,
              "request_id": request_id,
              "question": question,
              "status": result.get("status"),
              "blocker_id": blocker_id,
              "matched": result.get("status") == "answered" and blocker_id != UNKNOWN,
              "answer_excerpt": str(result.get("resolution") or "")[:240],
              "reason": ((result.get("oracle") or {}).get("reason")),
          })

    patch_path = attempt_dir / "patch.diff"
    files = patch_files(patch_path)
    patch_bytes = patch_path.stat().st_size if patch_path.exists() else 0
    normalized_questions = [re.sub(r"\s+", " ", q["question"]).strip().lower() for q in questions if q.get("question")]
    matched_blockers = sorted({q["blocker_id"] for q in questions if q["matched"]})
    flags = {
        "generated_assets": any("/public/assets/" in name or name.endswith("sandbox.js") or name.endswith(".map") for name in files),
        "lockfile": any(Path(name).name in {"yarn.lock", "package-lock.json", "pnpm-lock.yaml"} for name in files),
        "huge_diff": patch_bytes > 100_000,
        "empty_patch": patch_bytes == 0,
        "missing_tests": first_test is None,
        "missing_submit": not submitted,
        "sdk_error": bool(sdk_errors),
        "context_limit_or_timeout": context_limit,
    }
    return {
        **key,
        "model": attempt.get("model"),
        "trajectory": rel(path),
        "patch": rel(patch_path),
        "event_count": len(events),
        "question_count": len(questions),
        "relevant_question_count": sum(1 for q in questions if q["matched"]),
        "matched_blockers": matched_blockers,
        "duplicate_question_count": len(normalized_questions) - len(set(normalized_questions)),
        "irrelevant_question_count": sum(1 for q in questions if not q["matched"]),
        "first_repo_event": first_repo,
        "first_question_event": first_question,
        "first_test_event": first_test,
        "first_edit_event": first_edit,
        "questions_before_repo": sum(1 for q in questions if first_repo is None or q["event_index"] < first_repo),
        "questions_before_test": sum(1 for q in questions if first_test is None or q["event_index"] < first_test),
        "questions_after_edit": sum(1 for q in questions if first_edit is not None and q["event_index"] > first_edit),
        "patch_files": files,
        "patch_bytes": patch_bytes,
        "flags": flags,
        "sdk_errors": sdk_errors,
        "questions": questions,
    }


def load_blocker_counts(prepared_dir: Path) -> dict[str, int]:
    manifest = load_json(prepared_dir / "manifest.json") or {}
    counts = manifest.get("blocker_counts")
    if isinstance(counts, dict):
        return {str(key): int(value) for key, value in counts.items()}
    kb = load_json(prepared_dir / "kb.json") or {}
    out: dict[str, int] = defaultdict(int)
    for entry in kb.get("entries", []):
        out[str(entry.get("instance_id"))] += 1
    return dict(out)


def computed_process_from_audit(attempts: list[dict[str, Any]], blocker_counts: dict[str, int]) -> dict[str, Any]:
    by_harness: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in attempts:
        by_harness[item["harness"]].append(item)
    out = {}
    for harness, items in sorted(by_harness.items()):
        questions = sum(item["question_count"] for item in items)
        discovered = sum(len(item["matched_blockers"]) for item in items)
        present = sum(blocker_counts.get(item["instance_id"], 0) for item in items)
        precision = discovered / questions if questions else 0.0
        recall = discovered / present if present else 0.0
        ask_f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        out[harness] = {
            "question_count": questions,
            "blockers_discovered": discovered,
            "blockers_present": present,
            "precision": precision,
            "recall": recall,
            "ask_f1": ask_f1,
            "flagged_attempt_count": sum(1 for item in items if any(item["flags"].values())),
            "context_limit_or_timeout_count": sum(1 for item in items if item["flags"]["context_limit_or_timeout"]),
            "huge_or_generated_or_lockfile_count": sum(
                1 for item in items if item["flags"]["huge_diff"] or item["flags"]["generated_assets"] or item["flags"]["lockfile"]
            ),
        }
    return out


def pass_metric(item: dict[str, Any], k: int) -> Any:
    return (item.get("pass_at_k") or {}).get(str(k))


def unbiased_metric(item: dict[str, Any], k: int) -> Any:
    return (item.get("unbiased_pass_at_k") or {}).get(str(k))


def diagnostic_metric(item: dict[str, Any], k: int) -> Any:
    return (item.get("swebench_pro_test_pass_at_k") or {}).get(str(k))


def render_report(summary: dict[str, Any]) -> str:
    lines = [
        f"# Trust Horizon HiL-SWE {summary['selection_label']} Claude Code + Codex Report",
        "",
        f"- run_id: `{summary['run_id']}`",
        f"- prepared_dir: `{summary['prepared_dir']}`",
        f"- attempts audited: {len(summary['trajectory_audit'])}",
        f"- paper-pattern assessment: {summary['paper_pattern_assessment']}",
        "",
        "## Baseline Reference",
        "",
        "- SWE-agent GPT-5.5 xhigh first-3 ask_human smoke reference: pass@1 = 0/3, pass@3 = 1/3, Ask-F1 = 0.6588.",
        "- The first-3 reference is a smoke/pilot check, not a stable benchmark estimate for larger selections.",
        "",
        "## Harness Metrics",
        "",
        "| harness | HiL pass@1 | HiL pass@3 | diag pass@1 | diag pass@3 | unbiased HiL pass@3 | Ask-F1 | precision | recall | questions | blockers discovered / present | flags |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for harness, item in summary["harnesses"].items():
        audit = summary["audit_process"].get(harness, {})
        lines.append(
            "| "
            + " | ".join(
                [
                    harness,
                    fmt(pass_metric(item, 1)),
                    fmt(pass_metric(item, 3)),
                    fmt(diagnostic_metric(item, 1)),
                    fmt(diagnostic_metric(item, 3)),
                    fmt(unbiased_metric(item, 3)),
                    fmt(audit.get("ask_f1")),
                    fmt(audit.get("precision")),
                    fmt(audit.get("recall")),
                    fmt(audit.get("question_count")),
                    f"{audit.get('blockers_discovered', 0)} / {audit.get('blockers_present', 0)}",
                    fmt(audit.get("flagged_attempt_count")),
                ]
            )
            + " |"
        )
    lines.extend([
        "",
        "## Trajectory Audit",
        "",
        "| harness | attempt | task | q | matched | patch bytes | files | flags |",
        "| --- | ---: | --- | ---: | --- | ---: | --- | --- |",
    ])
    for item in summary["trajectory_audit"]:
        flags = [name for name, active in item["flags"].items() if active]
        files = ", ".join(item["patch_files"][:3]) + (" ..." if len(item["patch_files"]) > 3 else "")
        lines.append(
            f"| {item['harness']} | {item['attempt_index']} | {short_instance(item['instance_id'])} | "
            f"{item['question_count']} | {','.join(item['matched_blockers']) or '-'} | "
            f"{item['patch_bytes']} | {files or '-'} | {', '.join(flags) or '-'} |"
        )
    lines.extend(["", "## Questions"])
    for item in summary["trajectory_audit"]:
        lines.append("")
        lines.append(f"### {item['harness']} attempt {item['attempt_index']} {short_instance(item['instance_id'])}")
        if not item["questions"]:
            lines.append("- No clarification questions captured.")
            continue
        for q in item["questions"]:
            marker = q["blocker_id"] if q["matched"] else "irrelevant"
            lines.append(f"- `{marker}`: {q['question']}")
    lines.extend([
        "",
        "## Artifacts",
        "",
        f"- metrics: `{summary['artifacts'].get('metrics')}`",
        f"- process metrics: `{summary['artifacts'].get('process_metrics')}`",
        f"- trajectory audit: `{summary['artifacts'].get('trajectory_audit')}`",
        f"- final report json: `{summary['artifacts'].get('final_report_json')}`",
    ])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--prepared-dir", type=Path, required=True)
    parser.add_argument("--baseline-report", type=Path, default=None)
    parser.add_argument("--run-dir", type=Path, default=None)
    args = parser.parse_args()

    run_dir = args.run_dir or ROOT / "evals" / args.run_id
    metrics = load_json(run_dir / "metrics.json") or {}
    process = load_json(run_dir / "process_metrics.json") or {}
    progress = load_json(run_dir / "generation-progress.json") or {}
    blocker_counts = load_blocker_counts(args.prepared_dir)
    attempts = [audit_attempt(path, run_dir) for path in sorted((run_dir / "trajectories").glob("*/*/attempt-*/trajectory.jsonl"))]
    audit_process = computed_process_from_audit(attempts, blocker_counts)
    harnesses = metrics.get("harnesses") or {}
    low_pass = all((pass_metric(item, 3) or 0) <= 0.5 for item in harnesses.values()) if harnesses else False
    imperfect_ask = all((audit_process.get(harness, {}).get("ask_f1") or 0) < 0.9 for harness in harnesses) if harnesses else False
    if low_pass and imperfect_ask:
        assessment = "directionally aligned: low pass@3 and imperfect Ask-F1, with n=3 smoke/pilot caveat"
    elif harnesses:
        assessment = "deviates or is mixed relative to the expected SWE judgment-gap pattern; inspect harness rows"
    else:
        assessment = "metrics missing; cannot assess paper-pattern alignment"

    trajectory_audit_path = run_dir / "trajectory_audit.json"
    final_json_path = run_dir / "final_report.json"
    final_md_path = run_dir / "final_report.md"
    summary = {
        "run_id": args.run_id,
        "run_dir": rel(run_dir),
        "prepared_dir": rel(args.prepared_dir),
        "selection_label": prepared_label(args.prepared_dir),
        "baseline_report": str(args.baseline_report) if args.baseline_report else None,
        "generation_progress": progress,
        "harnesses": harnesses,
        "process_metrics_harnesses": process.get("harnesses") or {},
        "audit_process": audit_process,
        "paper_pattern_assessment": assessment,
        "trajectory_audit": attempts,
        "artifacts": {
            "metrics": rel(run_dir / "metrics.json"),
            "process_metrics": rel(run_dir / "process_metrics.json"),
            "trajectory_audit": rel(trajectory_audit_path),
            "final_report_json": rel(final_json_path),
            "final_report_md": rel(final_md_path),
        },
    }
    write_json(trajectory_audit_path, attempts)
    write_json(final_json_path, summary)
    final_md_path.write_text(render_report(summary), encoding="utf-8")
    print(final_md_path)
    print(final_json_path)
    expected_attempts = int(progress.get("total_jobs") or 0)
    if expected_attempts and len(attempts) != expected_attempts:
        raise SystemExit(f"Expected {expected_attempts} trajectories for this run; found {len(attempts)}")


if __name__ == "__main__":
    main()
