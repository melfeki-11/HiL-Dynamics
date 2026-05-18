#!/usr/bin/env python3
"""
Ingest HiL-Bench SWE tasks into trust_horizon's local task registry.

Public attempts are ingested from the ScaleAI/hil-bench HF dataset.
Private attempts are ingested via the paper pipeline's native attempt loader
(`create_data_object + setup_task_environment`), which reads source-of-truth
task data directly from backend storage (e.g., Mongo-backed attempt metadata)
and builds/reuses attempt-scoped `hilbench-swe:<attempt_id>` images with the
same setup logic used in the paper stack.

For each requested attempt (by uid/attempt_id), this script:
  1. Resolves attempt source (HF public row vs. private paper pipeline data)
  2. Ensures the task image is available locally (download/load for HF, setup/build/reuse for private)
  3. Ensures run_script.sh and parser.py are available
  4. Writes all metadata files under data/hil_bench_swe/tasks/<attempt_id>/

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
  python3 scripts/ingest_hil_swe.py --uids 69bc1094b455a91fa20fb868 69a9e77602049c14d2793bb5
  python3 scripts/ingest_hil_swe.py --all --p-set public
  python3 scripts/ingest_hil_swe.py --all --p-set private
  python3 scripts/ingest_hil_swe.py --all --p-set both
  python3 scripts/ingest_hil_swe.py --csv models/research_evals/hil_bench/utils/swe_delivered_tasks_and_attempts_PRIVATE.csv
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import types
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from tqdm import tqdm

# Canonical test command matching run_hil_bench.py
SWEAP_TEST_CMD = (
    "bash /root/run_script.sh > /tmp/stdout.log 2> /tmp/stderr.log; "
    "python /root/parser.py /tmp/stdout.log /tmp/stderr.log /tmp/output.json; "
    "python -c \"print('SWEAP_JSON_START'); print(open('/tmp/output.json').read()); print('SWEAP_JSON_END')\""
)
SWEAP_LOG_PARSER = "sweap_json"

HF_DATASET = "ScaleAI/hil-bench"
HF_TOKEN_FILE = Path.home() / ".cache" / "huggingface" / "stored_tokens"
_RESEARCH_EVALS_ENV: Path | None = None  # set via LITELLM_CREDENTIALS_FILE env var

DOCKER_LOADED_IMAGE_RE = re.compile(r"Loaded image:\s*(\S+)")
DOCKER_LOADED_IMAGE_ID_RE = re.compile(r"Loaded image ID:\s*(\S+)")

docker_load_lock = threading.Lock()

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "hil_bench_swe"
TASKS_DIR = DATA_DIR / "tasks"
IMAGES_CACHE_DIR = DATA_DIR / "image_archives"
SRC_ROOT = ROOT.parent
MODELS_ROOT = SRC_ROOT / "models"
PUBLIC_UIDS_CSV = (
    MODELS_ROOT
    / "research_evals"
    / "hil_bench"
    / "utils"
    / "swe_delivered_tasks_and_attempts_PUBLIC.csv"
)
PRIVATE_UIDS_CSV = (
    MODELS_ROOT
    / "research_evals"
    / "hil_bench"
    / "utils"
    / "swe_delivered_tasks_and_attempts_PRIVATE.csv"
)


@dataclass(frozen=True)
class WorkItem:
    uid: str
    source: str  # "hf" | "private"
    row: dict[str, Any] | None = None
    repo_or_db_name_hint: str = ""


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


def _ensure_models_import_paths() -> None:
    for path in (
        MODELS_ROOT,
        MODELS_ROOT / "genai",
        MODELS_ROOT / "research_evals" / "hil_bench",
        SRC_ROOT
        / "scaleapi"
        / "packages"
        / "customer-data-service"
        / "clients"
        / "customer_data_service_python_helper",
        SRC_ROOT
        / "scaleapi"
        / "packages"
        / "customer-data-service"
        / "clients"
        / "python",
        SRC_ROOT
        / "scaleapi"
        / "packages"
        / "s2sauth-helper-client"
        / "s2sauth_python_helper",
        SRC_ROOT
        / "scaleapi"
        / "packages"
        / "s2sauth"
        / "clients"
        / "python",
    ):
        s = str(path)
        if path.exists() and s not in sys.path:
            sys.path.insert(0, s)


def _ensure_botocore_vendored_requests_shim() -> None:
    """Provide botocore.vendored.requests shim for older internal imports."""
    try:
        import botocore.vendored  # type: ignore  # pragma: no cover

        return
    except Exception:
        pass
    try:
        import requests
    except Exception:
        return
    vendored = types.ModuleType("botocore.vendored")
    vendored.requests = requests
    sys.modules.setdefault("botocore.vendored", vendored)
    sys.modules.setdefault("botocore.vendored.requests", requests)


def _load_paper_pipeline_helpers():
    _ensure_models_import_paths()
    _ensure_botocore_vendored_requests_shim()
    try:
        from research_evals.hil_bench.utils.paper_pipeline import (  # type: ignore
            create_data_object,
            setup_task_environment,
            validate_swe_runtime_task,
        )
    except Exception as e:  # pragma: no cover - import errors are environment-dependent
        raise RuntimeError(
            "Failed importing paper_pipeline SWE helpers required for private ingest. "
            f"Expected repo path under {MODELS_ROOT}. Original error: {e}"
        ) from e
    return create_data_object, setup_task_environment, validate_swe_runtime_task


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


def load_csv_rows_by_uid(csv_path: Path) -> dict[str, dict[str, str]]:
    import csv

    rows: dict[str, dict[str, str]] = {}
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            uid = str(row.get("attempt_id", "")).strip()
            if uid:
                rows[uid] = row
    return rows


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
            cmd = f"zstd -dc {shlex.quote(str(archive_path))} | docker load"
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
    tmp.rename(destination)
    print(f"    Downloaded {bytes_written // 1024 // 1024} MB → {destination.name}", flush=True)


def ingest_task_from_hf(row: dict, token: str | None, skip_if_exists: bool) -> dict:
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
        "num_blockers": len(blocker_entries),
    }
    metadata_path.write_text(json.dumps(metadata, indent=2))
    print(f"    Written metadata ({len(blocker_entries)} blockers, {len(tests_to_pass)} tests)")
    return metadata


def ingest_task_from_private(
    uid: str,
    *,
    repo_or_db_name_hint: str,
    skip_if_exists: bool,
) -> dict:
    task_dir = TASKS_DIR / uid
    metadata_path = task_dir / "metadata.json"

    if skip_if_exists and metadata_path.exists():
        existing = json.loads(metadata_path.read_text())
        image_name = str(existing.get("image_name", "")).strip()
        if image_name and docker_image_exists(image_name):
            print(f"  [{uid}] Already ingested and image present, skipping.")
            return existing

    create_data_object, setup_task_environment, validate_swe_runtime_task = _load_paper_pipeline_helpers()

    print(f"  [{uid}] Ingesting private SWE attempt via paper pipeline ...")
    task_dir.mkdir(parents=True, exist_ok=True)
    ctx = None
    source_task_dir: Path | None = None

    try:
        attempt_data = create_data_object(uid, "swe")
        source_task_dir, _, ctx = setup_task_environment(
            attempt_data=attempt_data,
            task_type="swe",
        )
        if source_task_dir is None:
            validation_error = getattr(ctx, "validation_error", "") if ctx is not None else ""
            raise RuntimeError(
                f"paper_pipeline setup_task_environment returned no task_dir for {uid}. "
                f"validation_error={validation_error!r}"
            )
        source_task_dir = Path(source_task_dir)
        valid, validation_error = validate_swe_runtime_task(source_task_dir, uid)
        if not valid:
            raise RuntimeError(
                f"SWE runtime validation failed for {uid}: {validation_error or 'unknown'}"
            )

        source_metadata = json.loads((source_task_dir / "metadata.json").read_text())
        image_name = str(source_metadata.get("image_name", "")).strip()
        if not image_name:
            raise RuntimeError(f"Missing image_name in source metadata for {uid}")
        if not docker_image_exists(image_name):
            raise RuntimeError(f"Expected prebuilt image missing after setup: {image_name}")

        combined_problem_statement = (
            f"# PROBLEM STATEMENT\n{attempt_data.problem_statement}\n\n\n"
            f"# REQUIREMENTS\n{attempt_data.problem_requirements}\n\n\n"
            f"# PUBLIC INTERFACES\n{attempt_data.problem_interfaces}"
        )
        (task_dir / "problem_statement.txt").write_text(combined_problem_statement)

        for script_name in ("run_script.sh", "parser.py"):
            src = source_task_dir / script_name
            if not src.exists():
                raise FileNotFoundError(f"Missing required {script_name} at {src}")
            (task_dir / script_name).write_text(src.read_text())

        blocker_entries = normalize_blockers(getattr(attempt_data, "blocker_registry", []))
        blocker_registry = {"version": 1, "entries": blocker_entries}
        (task_dir / "blocker_registry.json").write_text(json.dumps(blocker_registry, indent=2))

        fail_to_pass = ensure_list_of_strings(
            (source_metadata.get("swe_bench_metadata") or {}).get("FAIL_TO_PASS")
            or getattr(attempt_data, "tests_to_pass", [])
        )
        test_files = ensure_list_of_strings(
            source_metadata.get("test_files")
            or getattr(attempt_data, "test_files", [])
        )
        metadata = {
            "instance_id": str(getattr(attempt_data, "instance_id")),
            "uid": uid,
            "repo_name": "app",
            "repo_or_db_name": str(repo_or_db_name_hint or getattr(attempt_data, "repo_name", "")),
            "base_commit": "HEAD",
            "image_name": image_name,
            "log_parser": str(source_metadata.get("log_parser") or SWEAP_LOG_PARSER),
            "test_cmd": str(source_metadata.get("test_cmd") or SWEAP_TEST_CMD),
            "test_patch": str(source_metadata.get("test_patch") or ""),
            "swe_bench_metadata": {
                "FAIL_TO_PASS": fail_to_pass,
                "PASS_TO_PASS": ensure_list_of_strings(
                    (source_metadata.get("swe_bench_metadata") or {}).get("PASS_TO_PASS") or []
                ),
            },
            "test_files": test_files,
            "num_blockers": len(blocker_entries),
        }
        if source_metadata.get("language"):
            metadata["language"] = str(source_metadata["language"])
        metadata_path.write_text(json.dumps(metadata, indent=2))
        print(f"    Prepared private task artifacts (image={image_name}, blockers={len(blocker_entries)})")
        return metadata
    except ModuleNotFoundError as e:
        missing_module = str(getattr(e, "name", "") or "").strip() or str(e)
        raise RuntimeError(
            "Private ingest requires the paper_pipeline runtime stack to be importable in the current "
            f"Python environment. Missing module: {missing_module}. "
            "Install missing internal deps (or run from the same env used by paper_pipeline) and retry."
        ) from e
    finally:
        if ctx is not None and hasattr(ctx, "cleanup"):
            try:
                ctx.cleanup()
            except Exception:
                pass


def load_hf_dataset(token: str | None) -> list[dict]:
    if token:
        os.environ["HF_TOKEN"] = token
    from datasets import load_dataset
    print(f"Loading HF dataset {HF_DATASET} ...", flush=True)
    ds = load_dataset(HF_DATASET, split="train")
    swe_rows = [dict(r) for r in ds if r.get("task_type", "").lower() == "swe"]
    print(f"Found {len(swe_rows)} SWE rows.")
    return swe_rows


def load_csv_uids(csv_path: Path) -> list[str]:
    return list(load_csv_rows_by_uid(csv_path).keys())


def load_uid_file(path: Path) -> list[str]:
    uids: list[str] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            uids.append(line)
    return uids


def write_tasks_index(results: list[dict]) -> None:
    index_path = DATA_DIR / "tasks_index.json"
    merged: dict[str, dict[str, Any]] = {}

    if index_path.exists():
        try:
            existing = json.loads(index_path.read_text())
            if isinstance(existing, list):
                for row in existing:
                    uid = str((row or {}).get("uid", "")).strip()
                    if uid:
                        merged[uid] = dict(row)
        except Exception:
            pass

    for m in results:
        uid = str(m.get("uid", "")).strip()
        if not uid:
            continue
        instance_id = m.get("instance_id", "")
        is_public = str(instance_id).startswith("public_")
        merged[uid] = {
            "uid": uid,
            "instance_id": instance_id,
            "is_public": is_public,
            "repo_or_db_name": m.get("repo_or_db_name"),
            "image_name": m.get("image_name"),
            "num_blockers": m.get("num_blockers", 0),
            "task_dir": str(TASKS_DIR / uid),
        }

    index = sorted(merged.values(), key=lambda row: str(row.get("uid", "")))
    index_path.write_text(json.dumps(index, indent=2))
    print(f"\nWrote tasks index: {index_path} ({len(index)} tasks total)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Ingest HiL-Bench SWE tasks into trust_horizon. "
            "Public attempts are read from HF; private attempts are prepared through paper_pipeline."
        )
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--uids", nargs="+", metavar="UID", help="Specific attempt UIDs to ingest.")
    group.add_argument(
        "--all",
        action="store_true",
        help="Ingest all attempts from --p-set (default: public).",
    )
    group.add_argument("--csv", type=Path, metavar="CSV_PATH",
                       help="CSV file with attempt_id column.")
    group.add_argument(
        "--uid-file",
        type=Path,
        metavar="PATH",
        help="Text file with one attempt UID per line (# comments allowed).",
    )
    parser.add_argument(
        "--p-set",
        choices=["public", "private", "both"],
        default="public",
        help=(
            "Partition set used by --all. "
            "'public' uses swe_delivered_tasks_and_attempts_PUBLIC.csv, "
            "'private' uses swe_delivered_tasks_and_attempts_PRIVATE.csv."
        ),
    )
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

    public_csv_rows = load_csv_rows_by_uid(PUBLIC_UIDS_CSV) if PUBLIC_UIDS_CSV.exists() else {}
    private_csv_rows = load_csv_rows_by_uid(PRIVATE_UIDS_CSV) if PRIVATE_UIDS_CSV.exists() else {}

    if args.all:
        target_uids: list[str] = []
        if args.p_set in {"public", "both"}:
            target_uids.extend(public_csv_rows.keys())
        if args.p_set in {"private", "both"}:
            target_uids.extend(private_csv_rows.keys())
    elif args.csv:
        target_uids = load_csv_uids(args.csv)
        print(f"Loaded {len(target_uids)} UIDs from CSV.")
    elif args.uid_file:
        target_uids = load_uid_file(args.uid_file)
        print(f"Loaded {len(target_uids)} UIDs from {args.uid_file}.")
    else:
        target_uids = args.uids

    # Preserve order while deduplicating.
    target_uids = list(dict.fromkeys(target_uids))
    if not target_uids:
        print("ERROR: No target UIDs resolved from arguments.", file=sys.stderr)
        sys.exit(1)

    # Determine whether we need HF rows at all.
    # Private-only runs can avoid loading HF entirely.
    def _source_hint(uid: str) -> str:
        in_public = uid in public_csv_rows
        in_private = uid in private_csv_rows
        if in_public and not in_private:
            return "hf"
        if in_private and not in_public:
            return "private"
        if in_public and in_private:
            if args.p_set == "private":
                return "private"
            return "hf"
        return "unknown"

    hints = {uid: _source_hint(uid) for uid in target_uids}
    need_hf = any(h in {"hf", "unknown"} for h in hints.values())
    rows_by_uid: dict[str, dict[str, Any]] = {}
    if need_hf:
        swe_rows = load_hf_dataset(token)
        rows_by_uid = {str(r["uid"]): r for r in swe_rows}

    work_items: list[WorkItem] = []
    unresolved: list[str] = []
    for uid in target_uids:
        hint = hints[uid]
        if hint == "hf":
            row = rows_by_uid.get(uid)
            if row is None:
                unresolved.append(uid)
                continue
            work_items.append(WorkItem(uid=uid, source="hf", row=row))
            continue
        if hint == "private":
            hint_repo = str(private_csv_rows.get(uid, {}).get("repo_or_db_name", ""))
            work_items.append(
                WorkItem(uid=uid, source="private", repo_or_db_name_hint=hint_repo)
            )
            continue

        # Unknown source: try HF first, then fall back to private paper-pipeline path.
        row = rows_by_uid.get(uid)
        if row is not None:
            work_items.append(WorkItem(uid=uid, source="hf", row=row))
        else:
            hint_repo = str(private_csv_rows.get(uid, {}).get("repo_or_db_name", ""))
            work_items.append(
                WorkItem(uid=uid, source="private", repo_or_db_name_hint=hint_repo)
            )

    if unresolved:
        print(f"ERROR: UIDs requested as public but not found in HF dataset: {unresolved}", file=sys.stderr)
        sys.exit(1)

    workers = args.workers if args.workers is not None else min(len(work_items), 4)
    print(f"\nIngesting {len(work_items)} task(s) with {workers} worker(s)...\n", flush=True)

    results = []
    errors = []

    def ingest_one(item: WorkItem) -> dict:
        if item.source == "hf":
            assert item.row is not None
            return ingest_task_from_hf(item.row, token, args.skip_if_exists)
        return ingest_task_from_private(
            item.uid,
            repo_or_db_name_hint=item.repo_or_db_name_hint,
            skip_if_exists=args.skip_if_exists,
        )

    if workers == 1:
        for item in tqdm(work_items, desc="Ingesting", unit="task"):
            try:
                metadata = ingest_one(item)
                results.append(metadata)
            except Exception as e:
                tqdm.write(f"  ERROR ingesting {item.uid}: {e}", file=sys.stderr)
                errors.append((item.uid, str(e)))
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(ingest_one, item): item.uid for item in work_items}
            with tqdm(total=len(work_items), desc="Ingesting", unit="task") as pbar:
                for future in as_completed(futures):
                    uid = futures[future]
                    try:
                        results.append(future.result())
                    except Exception as e:
                        tqdm.write(f"  ERROR ingesting {uid}: {e}", file=sys.stderr)
                        errors.append((uid, str(e)))
                    pbar.update(1)

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
