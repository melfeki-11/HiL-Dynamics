#!/usr/bin/env python3
"""Download a deterministic SWE-bench Pro sample."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from datasets import load_dataset

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = "ScaleAI/SWE-bench_Pro"
DEFAULT_SPLIT = "test"


def scalar_len(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(scalar_len(item) for item in value)
    if isinstance(value, dict):
        return sum(scalar_len(item) for item in value.values())
    return len(str(value))


def normalized_row(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for key, value in list(out.items()):
        if isinstance(value, (list, dict)):
            out[key] = json.dumps(value, sort_keys=True)
    return out


def row_score(row: dict[str, Any]) -> tuple[int, int, int, str]:
    selected_tests = row.get("selected_test_files_to_run") or ""
    selected_tests_count = len(selected_tests.splitlines()) or len(selected_tests.split(",")) if isinstance(selected_tests, str) else scalar_len(selected_tests)
    patch_size = scalar_len(row.get("patch")) + scalar_len(row.get("test_patch"))
    problem_size = scalar_len(row.get("problem_statement"))
    return (selected_tests_count, patch_size, problem_size, str(row.get("instance_id")))


def choose_rows(rows: list[dict[str, Any]], limit: int, repo: str | None, single_repo: bool) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        row_repo = str(row.get("repo") or "")
        if repo and row_repo != repo:
            continue
        groups[row_repo].append(row)
    if not groups:
        raise SystemExit(f"No rows found for repo={repo!r}")
    available_rows = [row for repo_rows in groups.values() for row in repo_rows]
    if len(available_rows) < limit:
        raise SystemExit(f"Only {len(available_rows)} rows available for repo={repo!r}; need {limit}.")

    if not single_repo:
        return sorted(available_rows, key=row_score)[:limit]

    def repo_score(item: tuple[str, list[dict[str, Any]]]) -> tuple[int, int, str]:
        repo_name, repo_rows = item
        docker_ready = sum(1 for row in repo_rows if row.get("dockerhub_tag"))
        return (docker_ready, len(repo_rows), repo_name)

    candidate_repo, candidate_rows = max(groups.items(), key=repo_score)
    if len(candidate_rows) < limit:
        raise SystemExit(f"Selected repo {candidate_repo} only has {len(candidate_rows)} rows; need {limit}.")
    return sorted(candidate_rows, key=row_score)[:limit]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = sorted({key for row in rows for key in row.keys()})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(normalized_row(row))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--split", default=DEFAULT_SPLIT)
    parser.add_argument("--repo", default=None, help="Optional exact repo name to force.")
    parser.add_argument("--single-repo", action="store_true", help="Select the low-friction sample from one repository instead of across all matching rows.")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "data")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    dataset = load_dataset(args.dataset, split=args.split)
    all_rows = [dict(row) for row in dataset]
    selected = choose_rows(all_rows, args.limit, args.repo, args.single_repo)

    jsonl_path = args.out_dir / "swebench_pro_samples.jsonl"
    csv_path = args.out_dir / "swebench_pro_samples.csv"
    manifest_path = args.out_dir / "sample_manifest.json"
    write_jsonl(jsonl_path, selected)
    write_csv(csv_path, selected)
    manifest = {
        "dataset": args.dataset,
        "split": args.split,
        "selection": "deterministic single-repo low-friction sample" if args.single_repo else "deterministic low-friction sample across matching rows",
        "limit": args.limit,
        "available_rows": len([row for row in all_rows if not args.repo or row.get("repo") == args.repo]),
        "repo": args.repo,
        "repos": dict(sorted(Counter(str(row.get("repo") or "") for row in selected).items())),
        "jsonl_path": str(jsonl_path),
        "csv_path": str(csv_path),
        "instances": [
            {"instance_id": row.get("instance_id"), "repo": row.get("repo"), "base_commit": row.get("base_commit"), "dockerhub_tag": row.get("dockerhub_tag")}
            for row in selected
        ],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    repos = ", ".join(f"{repo}={count}" for repo, count in manifest["repos"].items())
    print(f"Wrote {len(selected)} rows ({repos}):")
    print(f"  {jsonl_path}")
    print(f"  {csv_path}")
    print(f"  {manifest_path}")


if __name__ == "__main__":
    main()
