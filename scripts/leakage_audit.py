#!/usr/bin/env python3
"""Scan agent-visible attempt artifacts for hidden blocker registry leakage."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def hidden_terms(kb_path: Path, *, include_resolutions: bool = True) -> set[str]:
    parsed = load_json(kb_path)
    entries = parsed if isinstance(parsed, list) else parsed.get("entries", [])
    terms: set[str] = {
        "blocker_registry.json",
        "ask-human-data",
        "ask-human-cache.json",
        "human-kb.json",
        str(kb_path),
        kb_path.name,
    }
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "approval" or entry.get("decision") or entry.get("action_pattern"):
            continue
        keys = ["id", "blocker_id"]
        if include_resolutions:
            keys.append("resolution")
        for key in keys:
            value = str(entry.get(key) or "").strip()
            if len(value) >= 8:
                terms.add(value)
    return terms


PRIVATE_FILENAMES = {
    "trajectory.jsonl",
    "prediction.json",
    "patch.diff",
    "ask-human-cache.json",
    "human-kb.json",
    "leakage_audit.json",
}

SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


def is_candidate_file(path: Path) -> bool:
    if path.name in PRIVATE_FILENAMES or path.name.endswith(".lock"):
        return False
    if any(part in SKIP_DIRS for part in path.parts):
        return False
    try:
        return path.is_file() and path.stat().st_size < 1_000_000
    except OSError:
        return False


def candidate_files(run_dir: Path) -> list[Path]:
    files: list[Path] = []
    for attempt in sorted(path for path in (run_dir / "trajectories").rglob("attempt-*") if path.is_dir()):
        for name in ("prompt.md", "attempt.json", "env.json", "env_snapshot.json", "opencode_config.json"):
            path = attempt / name
            if path.exists():
                files.append(path)
        for private_config_dir in (attempt / ".config", attempt / ".home", attempt / ".local"):
            if private_config_dir.exists():
                files.extend(path for path in private_config_dir.rglob("*") if is_candidate_file(path))
        repo = attempt / "repo"
        if repo.exists():
            files.extend(path for path in repo.rglob("*") if is_candidate_file(path))
    return files


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument("--human-kb", type=Path, required=True)
    parser.add_argument("--mode", choices=["ask_human", "full_info", "baseline"], default=None)
    args = parser.parse_args()
    run_dir = args.run_dir or ROOT / "evals" / str(args.run_id)
    mode = args.mode
    if mode is None:
        try:
            mode = str(load_json(run_dir / "generation-progress.json").get("mode") or "")
        except Exception:
            mode = ""
    terms = hidden_terms(args.human_kb, include_resolutions=(mode != "full_info"))
    findings: list[dict[str, str]] = []
    for path in candidate_files(run_dir):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for term in terms:
            if term and term in text:
                findings.append({"file": str(path), "term": term})
                break
    out = {"run_dir": str(run_dir), "human_kb": str(args.human_kb), "mode": mode, "findings": findings}
    (run_dir / "leakage_audit.json").write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if findings:
        raise SystemExit(f"Leakage audit failed with {len(findings)} finding(s); see {run_dir / 'leakage_audit.json'}")
    print(run_dir / "leakage_audit.json")


if __name__ == "__main__":
    main()
