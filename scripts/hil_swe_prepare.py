#!/usr/bin/env python3
"""Prepare HiL-Bench SWE parquet rows for the existing SWE harnesses."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AUTONOMY_ROOT = Path(os.environ.get("AUTONOMY_CALIBRATION_ROOT", "/mnt/efs/mohamedelfeki/Codes/autonomy_calibration"))
DEFAULT_INPUT = DEFAULT_AUTONOMY_ROOT / "data" / "hil_bench" / "hil_swe_skyrl.zip"
DEFAULT_OUT = ROOT / "data" / "hil_bench_swe_first10"
DEFAULT_SOURCE_JSONL = DEFAULT_AUTONOMY_ROOT / "data" / "swebench_pro_samples.jsonl"
DEFAULT_SOURCE_CSV = DEFAULT_AUTONOMY_ROOT / "data" / "swebench_pro_samples.csv"
DEFAULT_BASELINE_MANIFEST = ROOT.parent / "hil-bench" / "local_data" / "hil_swe_first3" / "manifest.json"


def clean_value(value: Any) -> Any:
    if hasattr(value, "tolist"):
        return clean_value(value.tolist())
    if isinstance(value, dict):
        return {str(key): clean_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean_value(item) for item in value]
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            return clean_value(value.item())
        except Exception:
            pass
    return value


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_parquet_members(zip_path: Path) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    with zipfile.ZipFile(zip_path) as archive:
        for name in archive.namelist():
            if name.endswith(".parquet"):
                split = Path(name).stem
                frames[split] = pd.read_parquet(io.BytesIO(archive.read(name)))
    if not frames:
        raise SystemExit(f"No parquet files found in {zip_path}")
    return frames


def read_jsonl_by_id(path: Path) -> dict[str, dict[str, Any]]:
    rows = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            rows[str(row["instance_id"])] = row
    return rows


def raise_csv_field_limit() -> None:
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 10


def read_csv_by_id(path: Path) -> tuple[list[str], dict[str, dict[str, str]]]:
    raise_csv_field_limit()
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = {str(row["instance_id"]): row for row in reader}
        return list(reader.fieldnames or []), rows


def prompt_messages(row: pd.Series) -> list[dict[str, str]]:
    value = clean_value(row["prompt"])
    if not isinstance(value, list):
        raise ValueError("prompt column must contain a list of messages")
    return [dict(item) for item in value]


def user_prompt_text(messages: list[dict[str, str]]) -> str:
    for message in messages:
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return "\n\n".join(str(message.get("content") or "") for message in messages)


def extract_public_request(text: str) -> str:
    match = re.search(r"<request>\s*(.*?)\s*</request>", text, flags=re.S)
    return (match.group(1) if match else text).strip()


def clean_trigger_question(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(r"^[-*]\s*", "", text).strip()
    return text


def blocker_type(blocker: dict[str, Any]) -> str:
    text = f"{blocker.get('id', '')} {blocker.get('description', '')}".lower()
    if any(token in text for token in ("conflict", "contradict")):
        return "contradictory_information"
    if "ambiguous" in text:
        return "ambiguous_requirement"
    return "missing_information"


def convert_row(
    *,
    hil_row: pd.Series,
    split: str,
    row_index: int,
    source_row: dict[str, Any],
    source_csv_row: dict[str, str],
    input_zip: Path,
) -> tuple[dict[str, Any], dict[str, str], list[dict[str, Any]], dict[str, Any]]:
    extra_info = clean_value(hil_row["extra_info"])
    reward_spec = clean_value(hil_row["reward_spec"])
    messages = prompt_messages(hil_row)
    public_request = extract_public_request(user_prompt_text(messages))
    instance_id = str(extra_info["instance_id"])
    repo = str(extra_info.get("repo_name") or source_row.get("repo"))
    base_commit = str(extra_info.get("base_commit") or source_row.get("base_commit"))
    blockers = reward_spec.get("blockers") or []

    converted = {
        "repo": repo,
        "base_commit": base_commit,
        "instance_id": instance_id,
        "problem_statement": public_request,
        "repo_language": extra_info.get("language") or source_row.get("repo_language"),
        "hil_bench_mode": "blocked",
        "requirements": (
            "Use the public request and repository state. If a task-critical requirement cannot be determined from those sources, "
            "ask a concise targeted clarification and incorporate the answer."
        ),
        "hil_bench_split": split,
        "hil_bench_row_index": row_index,
        "hil_bench_task_id": extra_info.get("task_id"),
        "hil_bench_attempt_id": extra_info.get("attempt_id"),
        "hil_bench_source_zip": str(input_zip),
    }

    csv_row = dict(source_csv_row)
    csv_row["repo"] = repo
    csv_row["base_commit"] = base_commit
    csv_row["instance_id"] = instance_id
    csv_row["problem_statement"] = public_request
    csv_row["requirements"] = converted["requirements"]

    kb_entries = []
    oracle_blockers = []
    for blocker_index, blocker in enumerate(blockers):
        blocker = clean_value(blocker)
        original_id = str(blocker.get("id") or f"blocker-{blocker_index + 1}")
        trigger_questions = [
            clean_trigger_question(question)
            for question in blocker.get("example_questions", [])
            if clean_trigger_question(question)
        ]
        description = str(blocker.get("description") or "").strip()
        if description and description not in trigger_questions:
            trigger_questions.append(description)
        entry = {
            "id": original_id,
            "blocker_id": original_id,
            "instance_id": instance_id,
            "request_type": "clarification",
            "type": blocker_type(blocker),
            "description": description or original_id,
            "trigger_questions": trigger_questions or [description or original_id],
            "selector": description or original_id,
            "resolution": str(blocker.get("resolution") or ""),
            "resolution_source": blocker.get("resolution_source") or "human",
            "action_critical": bool(blocker.get("action_critical", True)),
            "observable_after": blocker.get("observable_after"),
            "commit_boundary": blocker.get("commit_boundary"),
            "metadata": {
                "source_zip": str(input_zip),
                "split": split,
                "row_index": row_index,
                "blocker_index": blocker_index,
                "env_class": hil_row.get("env_class"),
                "extra_info": extra_info,
                "raw_keys": sorted(blocker.keys()),
            },
        }
        kb_entries.append(entry)
        oracle_blockers.append(blocker)

    oracle = {
        "instance_id": instance_id,
        "repo": repo,
        "base_commit": base_commit,
        "split": split,
        "row_index": row_index,
        "env_class": hil_row.get("env_class"),
        "extra_info": extra_info,
        "prompt_messages": messages,
        "blockers": oracle_blockers,
        "ground_truth_patch": reward_spec.get("ground_truth_patch"),
        "num_blockers": reward_spec.get("num_blockers", len(blockers)),
    }
    return converted, csv_row, kb_entries, oracle


def full_info_problem_statement(problem_statement: str, blockers: list[dict[str, Any]]) -> str:
    lines = [
        problem_statement.rstrip(),
        "",
        "---",
        "",
        "## Additional Context",
        "",
        "The following clarifications are provided to help you complete this task:",
        "",
    ]
    for blocker in blockers:
        description = str(blocker.get("description") or blocker.get("id") or "Clarification").strip()
        resolution = str(blocker.get("resolution") or "").strip()
        lines.extend([f"### {description}", "", resolution, ""])
    return "\n".join(lines).rstrip() + "\n"


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def schema_summary(frames: dict[str, pd.DataFrame]) -> dict[str, Any]:
    summary = {}
    for split, frame in sorted(frames.items()):
        reward_keys = sorted({key for value in frame["reward_spec"] for key in clean_value(value).keys()})
        extra_keys = sorted({key for value in frame["extra_info"] for key in clean_value(value).keys()})
        env_counts = Counter(str(value) for value in frame["env_class"])
        summary[split] = {
            "rows": int(len(frame)),
            "columns": list(frame.columns),
            "env_class_counts": dict(sorted(env_counts.items())),
            "reward_spec_keys": reward_keys,
            "extra_info_keys": extra_keys,
        }
    return summary


def render_schema_note(manifest: dict[str, Any]) -> str:
    alignment = manifest.get("hil_evaluator_alignment") or {}
    counts = alignment.get("counts") or {}
    lines = [
        f"# HiL-Bench SWE First-{manifest['limit']} Preparation",
        "",
        f"- source zip: `{manifest['source_zip']}`",
        f"- selected split: `{manifest['selected_split']}`",
        f"- selected rows: {manifest['limit']}",
        f"- blocked/ask_human output jsonl: `{manifest['input_jsonl']}`",
        f"- full-info output jsonl: `{manifest['full_info_input_jsonl']}`",
        f"- output KB: `{manifest['kb_json']}`",
        f"- output samples CSV: `{manifest['samples_csv']}`",
        "",
        "## Archive Schema",
    ]
    for split, item in manifest["schema"].items():
        lines.append(
            f"- `{split}.parquet`: rows={item['rows']}; columns={', '.join(item['columns'])}; "
            f"reward_spec={', '.join(item['reward_spec_keys'])}; extra_info={', '.join(item['extra_info_keys'])}; "
            f"env_class_counts={item['env_class_counts']}"
        )
    lines.extend(["", "## Selected Instance IDs"])
    lines.extend(f"- {instance_id}" for instance_id in manifest["selected_instance_ids"])
    lines.extend(
        [
            "",
            "## Conversion Notes",
            "- The evaluator CSV preserves the existing SWE-Bench Pro hidden-test fields; the generation JSONL does not include them.",
            "- Important caveat: the SWE-Bench Pro evaluator fields are runnable functional tests, but they are treated as HiL outcome evaluators only when their sample patch exactly matches the HiL oracle patch.",
            f"- HiL evaluator alignment: aligned={counts.get('aligned', 0)}, missing_aligned_tests={counts.get('missing_aligned_tests', 0)}, comparable_mismatches={counts.get('comparable_mismatches', 0)}.",
            "- The generation JSONL intentionally excludes evaluator-only SWE metadata such as target test checkout commands, selected hidden tests, gold patches, and Docker tags.",
            "- `problem_statement` is replaced with the public underspecified HiL `<request>` body.",
            "- In blocked/ask_human input, blocker resolutions are written only to the `ask_human` KB and oracle sidecar, not to generation prompts.",
            "- In full-info input, blocker descriptions and resolutions are appended using the upstream `problem_full_info.jinja2` structure.",
            "- The SWE zip contains only parquet train/validation files; no standalone evaluator or test files were present.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--split", default="train")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--source-jsonl", type=Path, default=DEFAULT_SOURCE_JSONL)
    parser.add_argument("--source-csv", type=Path, default=DEFAULT_SOURCE_CSV)
    parser.add_argument(
        "--baseline-manifest",
        type=Path,
        default=DEFAULT_BASELINE_MANIFEST,
        help="Optional SWE-agent baseline manifest. When it exists, selected IDs and blocker counts must match.",
    )
    args = parser.parse_args()

    frames = load_parquet_members(args.input)
    if args.split not in frames:
        raise SystemExit(f"Split {args.split!r} not found in {args.input}; available: {sorted(frames)}")
    frame = frames[args.split]
    if args.limit < 1 or args.limit > len(frame):
        raise SystemExit(f"--limit must be between 1 and {len(frame)}")

    source_jsonl = read_jsonl_by_id(args.source_jsonl)
    csv_fieldnames, source_csv = read_csv_by_id(args.source_csv)
    selected = frame.iloc[: args.limit]

    converted_rows: list[dict[str, Any]] = []
    csv_rows: list[dict[str, str]] = []
    kb_entries: list[dict[str, Any]] = []
    oracle_rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for row_index, (_, hil_row) in enumerate(selected.iterrows()):
        extra_info = clean_value(hil_row["extra_info"])
        instance_id = str(extra_info["instance_id"])
        if instance_id not in source_jsonl or instance_id not in source_csv:
            missing.append(instance_id)
            continue
        converted, csv_row, entries, oracle = convert_row(
            hil_row=hil_row,
            split=args.split,
            row_index=row_index,
            source_row=source_jsonl[instance_id],
            source_csv_row=source_csv[instance_id],
            input_zip=args.input,
        )
        converted_rows.append(converted)
        csv_rows.append(csv_row)
        kb_entries.extend(entries)
        oracle_rows.append(oracle)
    if missing:
        raise SystemExit(f"Missing matching SWE-Bench Pro rows for: {missing}")

    input_jsonl = args.out / "input.jsonl"
    full_info_input_jsonl = args.out / "input_full_info.jsonl"
    samples_csv = args.out / "samples.csv"
    kb_json = args.out / "kb.json"
    oracle_jsonl = args.out / "oracle.jsonl"
    manifest_json = args.out / "manifest.json"
    schema_note = args.out / "schema_note.md"

    write_jsonl(input_jsonl, converted_rows)
    full_info_rows = []
    for row, oracle in zip(converted_rows, oracle_rows, strict=True):
        full_row = dict(row)
        full_row["hil_bench_mode"] = "full_info"
        full_row["problem_statement"] = full_info_problem_statement(row["problem_statement"], oracle.get("blockers") or [])
        full_row["requirements"] = "Use the public request, repository state, and the additional clarifications provided in this prompt."
        full_info_rows.append(full_row)
    write_jsonl(full_info_input_jsonl, full_info_rows)
    write_csv(samples_csv, csv_fieldnames, csv_rows)
    write_jsonl(oracle_jsonl, oracle_rows)
    kb = {
        "version": 1,
        "description": "HiL-Bench SWE blocker registry converted for deterministic ask_human.",
        "source_zip": str(args.input),
        "split": args.split,
        "limit": args.limit,
        "entries": kb_entries,
        "approval_entries": [],
    }
    kb_json.parent.mkdir(parents=True, exist_ok=True)
    kb_json.write_text(json.dumps(kb, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    selected_ids = [row["instance_id"] for row in converted_rows]
    blocker_counts = Counter(entry["instance_id"] for entry in kb_entries)
    patch_mismatches = []
    alignment_by_instance: dict[str, dict[str, Any]] = {}
    alignment_counts = {"aligned": 0, "missing_aligned_tests": 0, "comparable_mismatches": 0}
    for csv_row, oracle in zip(csv_rows, oracle_rows, strict=True):
        sample_patch = str(csv_row.get("patch") or "").strip()
        oracle_patch = str(oracle.get("ground_truth_patch") or "").strip()
        instance_id = str(oracle["instance_id"])
        comparable = bool(sample_patch and oracle_patch)
        exact_match = comparable and sample_patch == oracle_patch
        status = "aligned" if exact_match else "missing_aligned_tests"
        if exact_match:
            alignment_counts["aligned"] += 1
        else:
            alignment_counts["missing_aligned_tests"] += 1
            if comparable:
                alignment_counts["comparable_mismatches"] += 1
                patch_mismatches.append(instance_id)
        alignment_by_instance[instance_id] = {
            "hil_evaluator_status": status,
            "patch_exact_match": exact_match,
            "patches_comparable": comparable,
            "oracle_patch_sha256": sha256_text(oracle_patch) if oracle_patch else None,
            "sample_patch_sha256": sha256_text(sample_patch) if sample_patch else None,
        }
    manifest = {
        "source_zip": str(args.input),
        "sql_zip_seen_but_not_used": str(ROOT / "data" / "hil_bench" / "hil_sql_skyrl.zip"),
        "selected_split": args.split,
        "limit": args.limit,
        "selection_rule": f"first {args.limit} rows from {args.split}.parquet in archive order",
        "selected_instance_ids": selected_ids,
        "blocker_counts": dict(sorted(blocker_counts.items())),
        "total_blockers": sum(blocker_counts.values()),
        "schema": schema_summary(frames),
        "input_jsonl": str(input_jsonl),
        "full_info_input_jsonl": str(full_info_input_jsonl),
        "samples_csv": str(samples_csv),
        "kb_json": str(kb_json),
        "oracle_jsonl": str(oracle_jsonl),
        "oracle_sample_patch_alignment": {
            "compared": len(oracle_rows),
            "exact_matches": alignment_counts["aligned"],
            "mismatches": patch_mismatches,
        },
        "hil_evaluator_alignment": {
            "status_policy": "aligned only when samples.csv patch exactly matches reward_spec.ground_truth_patch; otherwise headline HiL-SWE outcome pass@k counts attempts as fail",
            "counts": alignment_counts,
            "by_instance": alignment_by_instance,
        },
        "assumptions": [
            "The SWE archive is the source of underspecified public prompts and blocker registries.",
            "Existing SWE-Bench Pro sample rows provide runnable official evaluator metadata for matching instance_ids, but those tests can underconstrain HiL blocker resolutions.",
            "The parquet env_class value is preserved as observed, even though it is `hil_sql_agent` in the SWE archive.",
        ],
    }
    baseline_manifest = args.baseline_manifest
    if baseline_manifest and baseline_manifest.exists():
        baseline = json.loads(baseline_manifest.read_text(encoding="utf-8"))
        baseline_ids = [str(item) for item in baseline.get("selected_instance_ids", [])]
        if baseline_ids and selected_ids != baseline_ids[: len(selected_ids)]:
            raise SystemExit(
                "Prepared selected instance IDs do not preserve the SWE-agent baseline prefix:\n"
                f"baseline_prefix={baseline_ids[: len(selected_ids)]}\nprepared={selected_ids}"
            )
        baseline_counts = {str(key): int(value) for key, value in (baseline.get("blocker_counts") or {}).items()}
        selected_baseline_counts = {key: value for key, value in baseline_counts.items() if key in selected_ids}
        prepared_counts = dict(sorted(blocker_counts.items()))
        baseline_count_mismatches = {
            key: {"baseline": value, "prepared": prepared_counts.get(key)}
            for key, value in selected_baseline_counts.items()
            if prepared_counts.get(key) != value
        }
        if baseline_count_mismatches:
            raise SystemExit(
                "Prepared blocker counts do not preserve the SWE-agent baseline prefix:\n"
                f"mismatches={baseline_count_mismatches}"
            )
        baseline_total = baseline.get("total_blockers")
        selected_baseline_total = sum(selected_baseline_counts.values())
        prepared_baseline_total = sum(prepared_counts.get(key, 0) for key in selected_baseline_counts)
        if baseline_total is not None and selected_baseline_total != prepared_baseline_total:
            raise SystemExit(
                f"Prepared baseline-prefix total blockers {prepared_baseline_total} does not match selected SWE-agent baseline {selected_baseline_total}"
            )
        manifest["swe_agent_baseline_manifest"] = str(baseline_manifest)
        manifest["swe_agent_baseline_prefix_checked"] = {
            "selected_ids": min(len(selected_ids), len(baseline_ids)),
            "blocker_count_instances": len(selected_baseline_counts),
            "total_blockers": selected_baseline_total,
        }
    manifest_json.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    schema_note.write_text(render_schema_note(manifest), encoding="utf-8")

    print(input_jsonl)
    print(samples_csv)
    print(kb_json)
    print(manifest_json)


if __name__ == "__main__":
    main()
