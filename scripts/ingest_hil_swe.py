#!/usr/bin/env python3
"""
Ingest HiL-Bench SWE tasks from HuggingFace into trust_horizon's data directory.

For each requested attempt (by uid/attempt_id), this script:
  1. Loads the task record from the ScaleAI/hil-bench HF dataset
  2. Downloads the Docker image archive (tar.zst) from HF buckets
  3. docker loads the image and records the resulting image name
  4. Extracts run_script.sh and parser.py from the loaded image
  5. Writes all metadata files under data/hil_bench_swe/tasks/<attempt_id>/

Output layout per task:
  data/hil_bench_swe/tasks/<attempt_id>/
    metadata.json           image_name, test_cmd, test_patch, tests_to_pass, test_files, uid, repo_name
    problem_statement.txt   raw problem statement
    blocker_registry.json   {"version":1,"entries":[...]}  (trust_horizon KB format)
    run_script.sh           extracted from Docker image /root/run_script.sh
    parser.py               extracted from Docker image /root/parser.py

A summary index is written to:
  data/hil_bench_swe/tasks_index.json

Usage:
  python3 scripts/ingest_hil_swe.py --uids 69bc1094b455a91fa20fb868 69a9e77602049c14d2793bb5 69c60cc7b6a31e9900faa779
  python3 scripts/ingest_hil_swe.py --all          # all 100 public SWE tasks
  python3 scripts/ingest_hil_swe.py --csv models/research_evals/hil_bench/utils/swe_delivered_tasks_and_attempts_PUBLIC.csv
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

# Canonical test command matching run_hil_bench.py
SWEAP_TEST_CMD = (
    "bash /root/run_script.sh > /tmp/stdout.log 2> /tmp/stderr.log; "
    "python /root/parser.py /tmp/stdout.log /tmp/stderr.log /tmp/output.json; "
    "python -c \"print('SWEAP_JSON_START'); print(open('/tmp/output.json').read()); print('SWEAP_JSON_END')\""
)
SWEAP_LOG_PARSER = "sweap_json"

HF_DATASET = "ScaleAI/hil-bench"
HF_TOKEN_FILE = Path.home() / ".cache" / "huggingface" / "stored_tokens"
# Canonical location for the research_evals HF token
_RESEARCH_EVALS_ENV = Path("/mnt/efs/tutrinh/src/models/research_evals/hil_bench/.env")

DOCKER_LOADED_IMAGE_RE = re.compile(r"Loaded image:\s*(\S+)")
DOCKER_LOADED_IMAGE_ID_RE = re.compile(r"Loaded image ID:\s*(\S+)")

docker_load_lock = threading.Lock()

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "hil_bench_swe"
TASKS_DIR = DATA_DIR / "tasks"
IMAGES_CACHE_DIR = DATA_DIR / "image_archives"


def _parse_token_from_env_file(path: Path) -> str | None:
    """Extract HF_TOKEN from a .env-style file."""
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("hf_"):
                return line
            if "=" in line:
                key, _, val = line.partition("=")
                key = key.strip().lstrip("export").strip()
                val = val.strip().strip('"').strip("'")
                if key in ("HF_TOKEN", "HUGGINGFACE_TOKEN", "hf_token", "token") and val.startswith("hf_"):
                    return val
    except Exception:
        pass
    return None


def read_hf_token() -> str | None:
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")
    if token:
        return token
    # Try the research_evals .env first (primary source)
    token = _parse_token_from_env_file(_RESEARCH_EVALS_ENV)
    if token:
        return token
    # Fall back to HF CLI stored token
    token = _parse_token_from_env_file(HF_TOKEN_FILE)
    if token:
        return token
    return None


def _normalize_blocker_entry(entry: dict) -> dict:
    """Convert a hil_bench blocker entry to trust_horizon KB format.

    hil_bench uses: id, description, resolution, example_questions
    trust_horizon uses: blocker_id, description, resolution, trigger_questions
    We write both field names so the KB is compatible with any reader.
    """
    out = dict(entry)
    # Ensure blocker_id is set (trust_horizon primary key)
    if "blocker_id" not in out and "id" in out:
        out["blocker_id"] = out["id"]
    # Map example_questions -> trigger_questions (trust_horizon field name)
    if "trigger_questions" not in out:
        eq = out.get("example_questions") or out.get("acceptable_questions") or []
        out["trigger_questions"] = [str(q) for q in eq if str(q).strip()]
    # Keep example_questions as well for backward compat with any Python readers
    return out


def normalize_blockers(raw: Any) -> list[dict]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [_normalize_blocker_entry(dict(b)) for b in raw]
    if isinstance(raw, dict):
        items = raw.get("blockers") or raw.get("entries") or []
        return [_normalize_blocker_entry(dict(b)) for b in items]
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [_normalize_blocker_entry(dict(b)) for b in parsed]
        if isinstance(parsed, dict):
            items = parsed.get("blockers") or parsed.get("entries") or []
            return [_normalize_blocker_entry(dict(b)) for b in items]
    return []


def ensure_list_of_strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        parsed = json.loads(stripped) if stripped.startswith("[") else [stripped]
        return [str(v) for v in parsed if str(v).strip()]
    return [str(value)]


def docker_image_exists(image_name: str) -> bool:
    result = subprocess.run(
        ["docker", "image", "inspect", image_name],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def load_docker_image_from_archive(archive_path: Path) -> str:
    with docker_load_lock:
        if archive_path.suffix == ".zst" or ".tar.zst" in archive_path.name:
            cmd = f"zstd -dc {archive_path} | docker load"
            result = subprocess.run(
                cmd, shell=True, executable="/bin/bash",
                capture_output=True, text=True,
            )
        else:
            result = subprocess.run(
                ["docker", "load", "-i", str(archive_path)],
                capture_output=True, text=True,
            )
    if result.returncode != 0:
        raise RuntimeError(
            f"docker load failed for {archive_path}: {result.stderr.strip() or result.stdout.strip()}"
        )
    combined = f"{result.stdout}\n{result.stderr}"
    matches = DOCKER_LOADED_IMAGE_RE.findall(combined)
    if matches:
        return matches[-1].strip()
    id_matches = DOCKER_LOADED_IMAGE_ID_RE.findall(combined)
    if id_matches:
        image_id = id_matches[-1].strip()
        inspect = subprocess.run(
            ["docker", "image", "inspect", image_id, "--format", "{{json .RepoTags}}"],
            capture_output=True, text=True,
        )
        if inspect.returncode == 0:
            try:
                repo_tags = json.loads(inspect.stdout.strip() or "[]")
                for tag in repo_tags:
                    if isinstance(tag, str) and tag and tag != "<none>:<none>":
                        return tag
            except Exception:
                pass
        return image_id
    raise ValueError(
        f"Could not find loaded image name in docker load output for {archive_path}.\n{combined}"
    )


def extract_scripts_from_image(image_name: str, task_dir: Path) -> None:
    for script_name in ("run_script.sh", "parser.py"):
        dest = task_dir / script_name
        if dest.exists():
            continue
        try:
            result = subprocess.run(
                ["docker", "run", "--rm", "--entrypoint", "", image_name,
                 "cat", f"/root/{script_name}"],
                capture_output=True, text=True, check=False,
            )
            if result.returncode == 0 and result.stdout:
                dest.write_text(result.stdout)
                print(f"    Extracted {script_name} from image")
            else:
                print(f"    Warning: could not extract {script_name} from image: {result.stderr[:200]}")
        except Exception as e:
            print(f"    Warning: failed to extract {script_name}: {e}")


def download_hf_file(hf_uri: str, destination: Path, token: str | None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        print(f"    Archive already cached: {destination.name}", flush=True)
        return
    print(f"    Downloading {hf_uri} ...", flush=True)
    from huggingface_hub import HfFileSystem
    fs = HfFileSystem(token=token)
    # hf://buckets/ScaleAI/... → buckets/ScaleAI/...
    fs_path = hf_uri.removeprefix("hf://")
    tmp = destination.with_suffix(".tmp")
    chunk_size = 8 * 1024 * 1024  # 8 MB chunks
    bytes_written = 0
    with fs.open(fs_path, "rb") as src, open(tmp, "wb") as dst:
        while True:
            chunk = src.read(chunk_size)
            if not chunk:
                break
            dst.write(chunk)
            bytes_written += len(chunk)
            if bytes_written % (64 * 1024 * 1024) < chunk_size:
                print(f"    ... {bytes_written // 1024 // 1024} MB", flush=True)
    tmp.rename(destination)
    print(f"    Downloaded {bytes_written // 1024 // 1024} MB → {destination.name}", flush=True)


def ingest_task(row: dict, token: str | None, skip_if_exists: bool) -> dict:
    uid = str(row["uid"])
    task_dir = TASKS_DIR / uid
    metadata_path = task_dir / "metadata.json"

    if skip_if_exists and metadata_path.exists():
        existing = json.loads(metadata_path.read_text())
        image_name = existing.get("image_name", "")
        if image_name and docker_image_exists(image_name):
            print(f"  [{uid}] Already ingested and image present, skipping.")
            return existing

    task_dir.mkdir(parents=True, exist_ok=True)
    print(f"  [{uid}] Ingesting {row.get('repo_or_db_name', '?')} ...")

    # Step 1: Download and load Docker image
    image_link = str(row["repo_or_db_download_link"])
    archive_name = f"{uid}.tar.zst"
    archive_path = IMAGES_CACHE_DIR / archive_name
    IMAGES_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if archive_path.exists():
        print(f"    Archive cached at {archive_path}")
    else:
        download_hf_file(image_link, archive_path, token)

    image_name = load_docker_image_from_archive(archive_path)
    print(f"    Loaded Docker image: {image_name}")

    # Step 2: Extract run_script.sh and parser.py
    extract_scripts_from_image(image_name, task_dir)

    # Step 3: Write problem_statement.txt
    (task_dir / "problem_statement.txt").write_text(str(row["problem"]))

    # Step 4: Write blocker_registry.json in trust_horizon KB format
    # {"entries": [...]} is the format loadHumanKnowledgeBase() in human_input.mjs expects.
    # Each entry has both blocker_id (trust_horizon) and id (hil_bench) for cross-compat.
    blocker_entries = normalize_blockers(row.get("blocker_registry"))
    blocker_registry = {"version": 1, "entries": blocker_entries}
    (task_dir / "blocker_registry.json").write_text(json.dumps(blocker_registry, indent=2))

    # Step 5: Write metadata.json
    tests_to_pass = ensure_list_of_strings(row.get("tests_to_pass"))
    test_files = ensure_list_of_strings(row.get("test_files"))
    test_patch = str(row.get("test_patch") or "")
    metadata = {
        "instance_id": str(row["task_id"]),      # public_swe_N
        "uid": uid,                               # attempt_id / image uid
        "repo_name": "app",                       # inside container the repo is at /testbed or similar
        "repo_or_db_name": str(row.get("repo_or_db_name", "")),
        "base_commit": "HEAD",
        "image_name": image_name,
        "log_parser": SWEAP_LOG_PARSER,
        "test_cmd": SWEAP_TEST_CMD,
        "test_patch": test_patch,
        "swe_bench_metadata": {
            "FAIL_TO_PASS": tests_to_pass,
            "PASS_TO_PASS": [],
        },
        "test_files": test_files,
        "num_blockers": len(blockers),
    }
    metadata_path.write_text(json.dumps(metadata, indent=2))
    print(f"    Written metadata ({len(blockers)} blockers, {len(tests_to_pass)} tests)")
    return metadata


def load_hf_dataset(token: str | None) -> list[dict]:
    if token:
        os.environ["HF_TOKEN"] = token
    from datasets import load_dataset
    print(f"Loading HF dataset {HF_DATASET} ...", flush=True)
    ds = load_dataset(HF_DATASET, split="train")
    swe_rows = [dict(r) for r in ds if r.get("task_type", "").lower() == "swe"]
    print(f"Found {len(swe_rows)} SWE rows.")
    return swe_rows


def load_public_csv_uids(csv_path: Path) -> list[str]:
    import csv
    uids = []
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            uid = row.get("attempt_id", "").strip()
            if uid:
                uids.append(uid)
    return uids


def write_tasks_index(results: list[dict]) -> None:
    index_path = DATA_DIR / "tasks_index.json"
    index = []
    for m in results:
        index.append({
            "uid": m.get("uid"),
            "instance_id": m.get("instance_id"),
            "repo_or_db_name": m.get("repo_or_db_name"),
            "image_name": m.get("image_name"),
            "num_blockers": m.get("num_blockers", 0),
            "task_dir": str(TASKS_DIR / m.get("uid", "")),
        })
    index_path.write_text(json.dumps(index, indent=2))
    print(f"\nWrote tasks index: {index_path} ({len(index)} tasks)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest HiL-Bench SWE tasks from HuggingFace.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--uids", nargs="+", metavar="UID", help="Specific attempt UIDs to ingest.")
    group.add_argument("--all", action="store_true", help="Ingest all 100 public SWE tasks.")
    group.add_argument("--csv", type=Path, metavar="CSV_PATH",
                       help="CSV file with attempt_id column (e.g. swe_delivered_tasks_and_attempts_PUBLIC.csv).")
    parser.add_argument("--skip-if-exists", action="store_true", default=True,
                        help="Skip tasks already ingested with image present (default: True).")
    parser.add_argument("--no-skip", dest="skip_if_exists", action="store_false",
                        help="Re-ingest even if already present.")
    parser.add_argument("--workers", type=int, default=None,
                        help="Parallel workers for downloading/loading images. "
                             "Defaults to min(num_tasks, 4). Downloads run concurrently; "
                             "docker loads are serialized automatically.")
    args = parser.parse_args()

    token = read_hf_token()
    if not token:
        print("Warning: No HF token found. Set HF_TOKEN env var or run `huggingface-cli login`.")

    swe_rows = load_hf_dataset(token)
    rows_by_uid = {str(r["uid"]): r for r in swe_rows}

    if args.all:
        target_uids = list(rows_by_uid.keys())
    elif args.csv:
        target_uids = load_public_csv_uids(args.csv)
        print(f"Loaded {len(target_uids)} UIDs from CSV.")
    else:
        target_uids = args.uids

    missing = [u for u in target_uids if u not in rows_by_uid]
    if missing:
        print(f"ERROR: UIDs not found in HF dataset: {missing}", file=sys.stderr)
        sys.exit(1)

    workers = args.workers if args.workers is not None else min(len(target_uids), 4)
    print(f"\nIngesting {len(target_uids)} task(s) with {workers} worker(s)...\n", flush=True)

    results = []
    errors = []

    def ingest_one(uid: str) -> dict:
        return ingest_task(rows_by_uid[uid], token, args.skip_if_exists)

    if workers == 1:
        for uid in target_uids:
            try:
                metadata = ingest_one(uid)
                results.append(metadata)
            except Exception as e:
                print(f"  ERROR ingesting {uid}: {e}", file=sys.stderr, flush=True)
                errors.append((uid, str(e)))
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(ingest_one, uid): uid for uid in target_uids}
            for future in as_completed(futures):
                uid = futures[future]
                try:
                    results.append(future.result())
                except Exception as e:
                    print(f"  ERROR ingesting {uid}: {e}", file=sys.stderr, flush=True)
                    errors.append((uid, str(e)))

    write_tasks_index(results)

    print(f"\n{'='*60}")
    print(f"Done: {len(results)} succeeded, {len(errors)} failed.")
    if errors:
        print("Failures:")
        for uid, err in errors:
            print(f"  {uid}: {err}")
        sys.exit(1)
    else:
        print(f"Tasks stored in: {TASKS_DIR}")


if __name__ == "__main__":
    main()
