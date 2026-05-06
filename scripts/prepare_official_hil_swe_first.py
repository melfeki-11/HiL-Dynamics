#!/usr/bin/env python3
"""Prepare a Trust Horizon fixture from the first local official HiL-SWE task."""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

from hil_swe_prepare import (
    DEFAULT_INPUT,
    DEFAULT_SOURCE_CSV,
    DEFAULT_SOURCE_JSONL,
    clean_value,
    convert_row,
    full_info_problem_statement,
    read_csv_by_id,
    read_jsonl_by_id,
    schema_summary,
    sha256_text,
    write_csv,
    write_jsonl,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TASKS_DIR = ROOT.parent / "hil-bench" / "local_data" / "hil_swe_first3" / "tasks"
DEFAULT_OUT = ROOT / "data" / "hil_bench_swe_official_first1"


def load_frames(zip_path: Path) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    with zipfile.ZipFile(zip_path) as archive:
        for name in archive.namelist():
            if name.endswith(".parquet"):
                frames[Path(name).stem] = pd.read_parquet(io.BytesIO(archive.read(name)))
    return frames


def first_task_dir(tasks_dir: Path, instance_id: str | None = None) -> Path:
    if instance_id:
        candidate = tasks_dir / instance_id
        if not candidate.exists():
            raise SystemExit(f"Requested official HiL-SWE task dir not found: {candidate}")
        return candidate
    candidates = sorted(path for path in tasks_dir.iterdir() if path.is_dir() and (path / "metadata.json").exists())
    if not candidates:
        raise SystemExit(f"No local official HiL-SWE task dirs found in {tasks_dir}")
    return candidates[0]


def find_hil_row(frames: dict[str, pd.DataFrame], instance_id: str) -> tuple[str, int, pd.Series]:
    for split, frame in frames.items():
        for row_index, (_, row) in enumerate(frame.iterrows()):
            extra_info = clean_value(row["extra_info"])
            if str(extra_info.get("instance_id")) == instance_id:
                return split, row_index, row
    raise SystemExit(f"Could not find {instance_id} in HiL-SWE archive")


def blocker_type(blocker: dict[str, Any]) -> str:
    text = f"{blocker.get('id', '')} {blocker.get('description', '')}".lower()
    if "conflict" in text or "contradict" in text:
        return "contradictory_information"
    if "ambiguous" in text:
        return "ambiguous_requirement"
    return str(blocker.get("type") or "missing_information")


def kb_entries_from_registry(blockers: list[dict[str, Any]], *, instance_id: str, task_dir: Path) -> list[dict[str, Any]]:
    entries = []
    for blocker_index, raw in enumerate(blockers):
        blocker = clean_value(raw)
        blocker_id = str(blocker.get("id") or f"blocker-{blocker_index + 1}")
        description = str(blocker.get("description") or blocker_id).strip()
        trigger_questions = [
            str(question).strip()
            for question in blocker.get("example_questions", [])
            if str(question).strip()
        ]
        if description and description not in trigger_questions:
            trigger_questions.append(description)
        entries.append(
            {
                "id": blocker_id,
                "blocker_id": blocker_id,
                "instance_id": instance_id,
                "request_type": "clarification",
                "type": blocker_type(blocker),
                "description": description,
                "trigger_questions": trigger_questions or [description],
                "selector": description,
                "resolution": str(blocker.get("resolution") or ""),
                "resolution_source": blocker.get("resolution_source") or "human",
                "action_critical": bool(blocker.get("action_critical", True)),
                "metadata": {
                    "source_task_dir": str(task_dir),
                    "blocker_index": blocker_index,
                },
            }
        )
    return entries


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks-dir", type=Path, default=DEFAULT_TASKS_DIR)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--source-jsonl", type=Path, default=DEFAULT_SOURCE_JSONL)
    parser.add_argument("--source-csv", type=Path, default=DEFAULT_SOURCE_CSV)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--instance-id", default=None)
    args = parser.parse_args()

    task_dir = first_task_dir(args.tasks_dir, args.instance_id)
    metadata = json.loads((task_dir / "metadata.json").read_text(encoding="utf-8"))
    instance_id = str(metadata["instance_id"])
    problem_statement = (task_dir / "problem_statement.txt").read_text(encoding="utf-8")
    blocker_registry = json.loads((task_dir / "blocker_registry.json").read_text(encoding="utf-8"))
    blockers = blocker_registry.get("blockers") or []

    frames = load_frames(args.input)
    split, row_index, hil_row = find_hil_row(frames, instance_id)
    source_jsonl = read_jsonl_by_id(args.source_jsonl)
    csv_fieldnames, source_csv = read_csv_by_id(args.source_csv)
    if instance_id not in source_jsonl or instance_id not in source_csv:
        raise SystemExit(f"Missing SWE-Bench Pro source row for {instance_id}")

    converted, csv_row, _entries, oracle = convert_row(
        hil_row=hil_row,
        split=split,
        row_index=row_index,
        source_row=source_jsonl[instance_id],
        source_csv_row=source_csv[instance_id],
        input_zip=args.input,
    )
    converted["problem_statement"] = problem_statement
    converted["hil_bench_mode"] = "blocked"
    converted["requirements"] = (
        "Use the public request and repository state. If a task-critical requirement cannot be determined from those sources, "
        "ask a concise targeted clarification and incorporate the answer."
    )
    csv_row["problem_statement"] = problem_statement
    csv_row["requirements"] = converted["requirements"]
    oracle["blockers"] = blockers
    oracle["local_official_task_dir"] = str(task_dir)
    entries = kb_entries_from_registry(blockers, instance_id=instance_id, task_dir=task_dir)

    args.out.mkdir(parents=True, exist_ok=True)
    input_jsonl = args.out / "input.jsonl"
    full_info_input_jsonl = args.out / "input_full_info.jsonl"
    samples_csv = args.out / "samples.csv"
    kb_json = args.out / "kb.json"
    oracle_jsonl = args.out / "oracle.jsonl"
    manifest_json = args.out / "manifest.json"
    tasks_out = args.out / "tasks"

    write_jsonl(input_jsonl, [converted])
    full_row = dict(converted)
    full_row["hil_bench_mode"] = "full_info"
    full_row["problem_statement"] = full_info_problem_statement(problem_statement, blockers)
    full_row["requirements"] = "Use the public request, repository state, and the additional clarifications provided in this prompt."
    write_jsonl(full_info_input_jsonl, [full_row])
    write_csv(samples_csv, csv_fieldnames, [csv_row])
    write_jsonl(oracle_jsonl, [oracle])
    kb = {
        "version": 1,
        "description": "HiL-Bench SWE blocker registry converted from the local official task directory.",
        "source_task_dir": str(task_dir),
        "entries": entries,
        "approval_entries": [],
    }
    kb_json.write_text(json.dumps(kb, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if tasks_out.exists() or tasks_out.is_symlink():
        if tasks_out.is_symlink() or tasks_out.is_file():
            tasks_out.unlink()
        else:
            shutil.rmtree(tasks_out)
    tasks_out.mkdir(parents=True, exist_ok=True)
    task_link = tasks_out / task_dir.name
    try:
        task_link.symlink_to(task_dir, target_is_directory=True)
    except OSError:
        shutil.copytree(task_dir, task_link)

    sample_patch = str(csv_row.get("patch") or "").strip()
    oracle_patch = str(oracle.get("ground_truth_patch") or "").strip()
    exact_match = bool(sample_patch and oracle_patch and sample_patch == oracle_patch)
    manifest = {
        "source": "local_official_hil_swe_task",
        "source_task_dir": str(task_dir),
        "source_zip": str(args.input),
        "selected_split": split,
        "selected_row_index": row_index,
        "selected_instance_ids": [instance_id],
        "blocker_counts": dict(Counter(entry["instance_id"] for entry in entries)),
        "total_blockers": len(entries),
        "input_jsonl": str(input_jsonl),
        "full_info_input_jsonl": str(full_info_input_jsonl),
        "samples_csv": str(samples_csv),
        "kb_json": str(kb_json),
        "oracle_jsonl": str(oracle_jsonl),
        "tasks_dir": str(tasks_out),
        "schema": schema_summary(frames),
        "official_task_image": metadata.get("image_name"),
        "hil_evaluator_alignment": {
            "status_policy": "aligned only when samples.csv patch exactly matches reward_spec.ground_truth_patch; otherwise headline HiL-SWE outcome pass@k counts attempts as fail",
            "counts": {
                "aligned": 1 if exact_match else 0,
                "missing_aligned_tests": 0 if exact_match else 1,
                "comparable_mismatches": 0 if exact_match else int(bool(sample_patch and oracle_patch)),
            },
            "by_instance": {
                instance_id: {
                    "hil_evaluator_status": "aligned" if exact_match else "missing_aligned_tests",
                    "patch_exact_match": exact_match,
                    "patches_comparable": bool(sample_patch and oracle_patch),
                    "oracle_patch_sha256": sha256_text(oracle_patch) if oracle_patch else None,
                    "sample_patch_sha256": sha256_text(sample_patch) if sample_patch else None,
                }
            },
        },
    }
    manifest_json.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(input_jsonl)
    print(samples_csv)
    print(kb_json)
    print(manifest_json)


if __name__ == "__main__":
    main()
