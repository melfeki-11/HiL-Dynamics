"""
Evaluation pipeline for trust_horizon HiL-SWE runs.

For each completed (uid, mode, pass) attempt that has a patch.diff, spins up a fresh
hilbench-swe:<uid> Docker container, applies the agent patch + the hidden test patch,
runs the SWEAP test command, and writes eval_result.json to the pass directory.

eval_result.json schema:
  {
    "uid":           str,
    "mode":          str,
    "pass_index":    int,
    "resolved":      bool,          # True iff all FAIL_TO_PASS tests passed
    "patch_applied": bool,          # False if git apply failed (error)
    "test_ran":      bool,          # False if SWEAP_TEST_CMD failed entirely
    "tests_to_pass": list[str],
    "passed_tests":  list[str],
    "failed_tests":  list[str],
    "all_tests":     list[{name, status}],
    "error":         str | null,
  }

Usage:
  # Evaluate specific run, all completed attempts:
  python3 scripts/eval_hil_swe.py --run-id my-run

  # Evaluate specific UIDs / modes / passes:
  python3 scripts/eval_hil_swe.py --run-id my-run \\
    --uids 69bc1094b455a91fa20fb868 \\
    --modes ask_human --passes 1

  # Re-evaluate even if eval_result.json already exists:
  python3 scripts/eval_hil_swe.py --run-id my-run --force

Docker cleanup:
  Eval containers use --rm so they are removed on exit.
  A run-owner token is registered at the start of the script; the cleanup helper
  in run_hil_swe.py (or a future gc script) uses it to avoid removing containers
  while this process is still alive.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "hil_bench_swe"
TASKS_DIR = DATA_DIR / "tasks"
TASKS_INDEX = DATA_DIR / "tasks_index.json"
RUNS_DIR = ROOT / "runs"

RUN_OWNER_DIR = Path(os.getenv("HIL_BENCH_RUN_OWNER_DIR", "/tmp/hil_bench_run_owners"))

# The eval container runs the base hilbench-swe image (not the harness).
# It applies patches and runs run_script.sh / parser.py that are already baked in.
SWEAP_TEST_CMD = (
    "bash /root/run_script.sh > /tmp/stdout.log 2> /tmp/stderr.log; "
    "python /root/parser.py /tmp/stdout.log /tmp/stderr.log /tmp/output.json; "
    "python -c \"print('SWEAP_JSON_START'); "
    "import json; print(json.dumps(json.load(open('/tmp/output.json')))); "
    "print('SWEAP_JSON_END')\""
)

SWEAP_JSON_START = "SWEAP_JSON_START"
SWEAP_JSON_END = "SWEAP_JSON_END"

_print_lock = threading.Lock()


def log(msg: str, file=sys.stdout) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    with _print_lock:
        print(f"[{ts}] {msg}", file=file, flush=True)


# ── Run-owner registration ──────────────────────────────────────────────────

def register_run_owner() -> Path:
    RUN_OWNER_DIR.mkdir(parents=True, exist_ok=True)
    token = RUN_OWNER_DIR / f"{os.getpid()}.owner"
    token.write_text(str(os.getpid()))
    return token


def unregister_run_owner(token: Path) -> None:
    token.unlink(missing_ok=True)


# ── SWEAP output parsing ────────────────────────────────────────────────────

def parse_sweap_json(output: str) -> list[dict]:
    """Extract and parse the SWEAP_JSON block from combined container output.

    Returns list of {name: str, status: str} dicts, or [] on parse failure.
    """
    start = output.find(SWEAP_JSON_START)
    end = output.find(SWEAP_JSON_END)
    if start == -1 or end == -1:
        return []
    json_str = output[start + len(SWEAP_JSON_START):end].strip()
    try:
        data = json.loads(json_str)
        return data.get("tests", [])
    except Exception:
        return []


def compute_resolved(tests: list[dict], tests_to_pass: list[str]) -> tuple[bool, list[str], list[str]]:
    """Return (resolved, passed_tests, failed_tests) given parsed test results.

    resolved = True iff every test in tests_to_pass has status PASSED.
    """
    if not tests_to_pass:
        # No target tests specified → can't resolve
        return False, [], []

    by_name: dict[str, str] = {t["name"]: t["status"] for t in tests if "name" in t}
    passed = [t for t in tests_to_pass if by_name.get(t, "MISSING") == "PASSED"]
    failed = [t for t in tests_to_pass if by_name.get(t, "MISSING") != "PASSED"]
    return len(failed) == 0 and len(passed) == len(tests_to_pass), passed, failed


# ── Core evaluation ─────────────────────────────────────────────────────────

def eval_attempt(
    *,
    uid: str,
    mode: str,
    pass_index: int,
    run_id: str,
    skip_if_complete: bool,
    timeout_s: int,
) -> tuple[bool, str]:
    """Evaluate one (uid, mode, pass_index) attempt.  Returns (success, message)."""
    pass_dir = RUNS_DIR / run_id / uid / mode / f"pass_{pass_index}"
    eval_path = pass_dir / "eval_result.json"

    if skip_if_complete and eval_path.exists():
        return True, f"[{uid[:12]}|{mode}|p{pass_index}] eval already exists, skipped"

    # Need solve result to exist
    result_path = pass_dir / "result.json"
    if not result_path.exists():
        return False, f"[{uid[:12]}|{mode}|p{pass_index}] result.json not found — run solve first"

    patch_path = pass_dir / "patch.diff"
    if not patch_path.exists():
        return False, f"[{uid[:12]}|{mode}|p{pass_index}] patch.diff not found"

    # Load task metadata
    task_dir = TASKS_DIR / uid
    metadata_path = task_dir / "metadata.json"
    if not metadata_path.exists():
        return False, f"[{uid[:12]}|{mode}|p{pass_index}] metadata.json not found — run ingest first"

    metadata = json.loads(metadata_path.read_text())
    base_image = metadata["image_name"]           # hilbench-swe:<uid>
    test_patch = metadata.get("test_patch", "")
    tests_to_pass: list[str] = metadata.get("swe_bench_metadata", {}).get("FAIL_TO_PASS", [])

    label = f"[{uid[:12]}|{mode}|p{pass_index}]"

    # Write patches to temp files so we can bind-mount them
    with tempfile.TemporaryDirectory(prefix=f"th_eval_{uid[:8]}_") as tmpdir:
        tmp = Path(tmpdir)
        agent_patch_file = tmp / "agent.patch"
        test_patch_file = tmp / "test.patch"

        agent_patch_file.write_text(patch_path.read_text())
        test_patch_file.write_text(test_patch)

        # Eval script: runs inside the container.
        # 1. Apply agent patch (best-effort — agent may not have produced changes)
        # 2. Apply test patch (hard-required for evaluation)
        # 3. Run SWEAP_TEST_CMD
        eval_script = r"""#!/bin/sh
set -e
cd /app

PATCH_APPLIED=0
if [ -s /tmp/agent.patch ]; then
  if git apply /tmp/agent.patch 2>/tmp/agent_patch.log; then
    PATCH_APPLIED=1
    echo "PATCH_APPLY_STATUS: ok"
  else
    echo "PATCH_APPLY_STATUS: failed"
    cat /tmp/agent_patch.log >&2
  fi
else
  echo "PATCH_APPLY_STATUS: empty"
  PATCH_APPLIED=1
fi

if git apply /tmp/test.patch 2>/tmp/test_patch.log; then
  echo "TEST_PATCH_STATUS: ok"
else
  echo "TEST_PATCH_STATUS: failed"
  cat /tmp/test_patch.log >&2
  exit 2
fi

""" + SWEAP_TEST_CMD
        eval_script_file = tmp / "eval.sh"
        eval_script_file.write_text(eval_script)

        cmd = [
            "docker", "run", "--rm",
            # bind-mount patches and eval script read-only
            "-v", f"{agent_patch_file}:/tmp/agent.patch:ro",
            "-v", f"{test_patch_file}:/tmp/test.patch:ro",
            "-v", f"{eval_script_file}:/tmp/eval.sh:ro",
            # No harness needed for eval — use the clean base image
            base_image,
            "sh", "/tmp/eval.sh",
        ]

        log(f"{label} Starting eval container ({base_image})")
        started_at = time.time()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            err = f"eval timed out after {timeout_s}s"
            log(f"{label} {err}", file=sys.stderr)
            _write_eval_result(eval_path, uid, mode, pass_index, error=err)
            return False, f"{label} {err}"
        except Exception as exc:
            err = str(exc)
            log(f"{label} Exception: {err}", file=sys.stderr)
            _write_eval_result(eval_path, uid, mode, pass_index, error=err)
            return False, f"{label} Exception: {err}"

        elapsed = int(time.time() - started_at)
        combined_output = result.stdout + "\n" + result.stderr

        # Determine patch apply status
        patch_applied = "PATCH_APPLY_STATUS: ok" in combined_output or "PATCH_APPLY_STATUS: empty" in combined_output
        test_ran = result.returncode in (0, 1)  # exit 0 = tests ran (all ok or some failed), exit 2 = test patch failed

        # Parse SWEAP JSON
        test_results = parse_sweap_json(combined_output)
        resolved, passed_tests, failed_tests = compute_resolved(test_results, tests_to_pass)

        if result.returncode not in (0, 1) and result.returncode != 0:
            error_msg = f"container exited {result.returncode}; stderr: {result.stderr[:500]}"
        else:
            error_msg = None

        eval_data = {
            "uid": uid,
            "mode": mode,
            "pass_index": pass_index,
            "resolved": resolved,
            "patch_applied": patch_applied,
            "test_ran": test_ran,
            "tests_to_pass": tests_to_pass,
            "passed_tests": passed_tests,
            "failed_tests": failed_tests,
            "all_tests": test_results,
            "container_exit_code": result.returncode,
            "elapsed_s": elapsed,
            "error": error_msg,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
        }
        eval_path.write_text(json.dumps(eval_data, indent=2))

        status_str = "RESOLVED ✓" if resolved else "unresolved"
        log(f"{label} {status_str} in {elapsed}s ({len(passed_tests)}/{len(tests_to_pass)} FAIL_TO_PASS tests)")
        return True, f"{label} {status_str}"


def _write_eval_result(path: Path, uid: str, mode: str, pass_index: int, error: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "uid": uid,
        "mode": mode,
        "pass_index": pass_index,
        "resolved": False,
        "patch_applied": False,
        "test_ran": False,
        "tests_to_pass": [],
        "passed_tests": [],
        "failed_tests": [],
        "all_tests": [],
        "container_exit_code": None,
        "elapsed_s": 0,
        "error": error,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2))


# ── Job discovery ───────────────────────────────────────────────────────────

def discover_jobs(
    run_dir: Path,
    uid_filter: list[str] | None,
    mode_filter: list[str] | None,
    passes: int | None,
    force: bool,
) -> list[dict]:
    """Scan run_dir and return a list of {uid, mode, pass_index} dicts that need eval."""
    jobs = []
    if not run_dir.exists():
        return jobs

    for uid_dir in sorted(run_dir.iterdir()):
        if not uid_dir.is_dir():
            continue
        uid = uid_dir.name
        if uid_filter and uid not in uid_filter:
            continue

        for mode_dir in sorted(uid_dir.iterdir()):
            if not mode_dir.is_dir():
                continue
            mode = mode_dir.name
            if mode_filter and mode not in mode_filter:
                continue

            for pass_dir in sorted(mode_dir.iterdir()):
                if not pass_dir.is_dir() or not pass_dir.name.startswith("pass_"):
                    continue
                try:
                    pass_idx = int(pass_dir.name[5:])
                except ValueError:
                    continue
                if passes is not None and pass_idx > passes:
                    continue
                if not (pass_dir / "result.json").exists():
                    continue  # Not yet solved
                if not force and (pass_dir / "eval_result.json").exists():
                    continue  # Already evaluated
                jobs.append({"uid": uid, "mode": mode, "pass_index": pass_idx})

    return jobs


# ── CLI ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate trust_horizon HiL-SWE solve attempts."
    )
    parser.add_argument("--run-id", required=True, help="Run identifier (subdirectory under runs/).")
    parser.add_argument("--uids", nargs="+", metavar="UID", help="Restrict evaluation to these UIDs.")
    parser.add_argument(
        "--modes", nargs="+", choices=["ask_human", "full_info"],
        help="Restrict to these modes (default: all).",
    )
    parser.add_argument(
        "--passes", type=int, default=None,
        help="Only evaluate up to this pass number (default: all).",
    )
    parser.add_argument("--force", action="store_true", help="Re-evaluate even if eval_result.json exists.")
    parser.add_argument(
        "--workers", "-w", type=int, default=None,
        help="Max concurrent eval containers (default: min(num_jobs, 8)).",
    )
    parser.add_argument(
        "--timeout", type=int, default=600,
        help="Per-attempt eval timeout in seconds (default: 600).",
    )
    args = parser.parse_args()

    run_dir = RUNS_DIR / args.run_id
    if not run_dir.exists():
        print(f"ERROR: Run directory not found: {run_dir}", file=sys.stderr)
        sys.exit(1)

    jobs = discover_jobs(
        run_dir,
        uid_filter=args.uids,
        mode_filter=args.modes,
        passes=args.passes,
        force=args.force,
    )

    if not jobs:
        log("No attempts to evaluate (all already have eval_result.json, or no solve results found).")
        return

    workers = args.workers if args.workers is not None else min(len(jobs), 8)
    log(f"Evaluating {len(jobs)} attempt(s) with {workers} worker(s) — run_id='{args.run_id}'")

    owner_token = register_run_owner()
    successes: list[str] = []
    failures: list[str] = []

    try:
        def run_one(job: dict) -> tuple[bool, str]:
            return eval_attempt(
                uid=job["uid"],
                mode=job["mode"],
                pass_index=job["pass_index"],
                run_id=args.run_id,
                skip_if_complete=not args.force,
                timeout_s=args.timeout,
            )

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(run_one, j): j for j in jobs}
            for future in as_completed(futures):
                ok, msg = future.result()
                (successes if ok else failures).append(msg)
    finally:
        unregister_run_owner(owner_token)

    log(f"\n{'='*60}")
    log(f"Done: {len(successes)} evaluated, {len(failures)} failed.")
    for msg in failures:
        log(f"  FAILED: {msg}", file=sys.stderr)
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
