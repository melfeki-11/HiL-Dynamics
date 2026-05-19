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
    --modes neutral --passes 1

  # Re-evaluate even if eval_result.json already exists:
  python3 scripts/eval_hil_swe.py --run-id my-run --force

Docker cleanup:
  Eval containers use the BASE hilbench-swe:<uid> image (not the harness image).
  run_hil_swe.py's cleanup_orphaned_containers only queries by ancestor=harness_image,
  so eval containers are never in its scope regardless of owner tokens.
  Eval containers are explicitly removed with `docker rm -f` after each attempt,
  and cleanup_orphaned_eval_containers() sweeps leftovers after interruptions.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "hil_bench_swe"
TASKS_DIR = DATA_DIR / "tasks"
TASKS_INDEX = DATA_DIR / "tasks_index.json"
RUNS_DIR = ROOT / "runs"


def _run_id_token(run_id: str) -> str:
    """Stable short token for container names, collision-resistant across run_ids."""
    return hashlib.sha1(str(run_id).encode("utf-8")).hexdigest()[:12]

def _default_run_owner_dir() -> Path:
    user = (os.getenv("USER") or getpass.getuser() or "unknown").strip()
    user = re.sub(r"[^A-Za-z0-9_.-]+", "_", user) or "unknown"
    return Path(f"/tmp/hil_bench_run_owners_{user}")


RUN_OWNER_DIR = Path(os.getenv("HIL_BENCH_RUN_OWNER_DIR") or str(_default_run_owner_dir()))

# The eval container runs the base hilbench-swe image (not the harness).
# It applies patches and runs run_script.sh / parser.py that are already baked in.
SWEAP_TEST_CMD = (
    "bash /root/run_script.sh > /tmp/stdout.log 2> /tmp/stderr.log; "
    "python /root/parser.py /tmp/stdout.log /tmp/stderr.log /tmp/output.json; "
    "python -c \"print('SWEAP_JSON_START'); print(open('/tmp/output.json').read()); print('SWEAP_JSON_END')\""
)

SWEAP_JSON_START = "SWEAP_JSON_START"
SWEAP_JSON_END = "SWEAP_JSON_END"

# Patterns for file diffs that the canonical filter_patch() strips before applying.
# Mirrors custom_eval.py PATCH_FILTER_PATTERNS exactly.
# Critically includes parser.py / run_script.sh (security) and __pycache__ (git apply safety).
_PATCH_FILTER_PATTERNS = [
    r"__pycache__/",
    r"node_modules/",
    r"\.egg-info/",
    r"diff --git a/\S+\.pyc ",
    r"diff --git a/\S+\.pyo ",
    r"diff --git a/\S+\.so ",
    r"diff --git a/\S+\.dll ",
    r"diff --git a/\S+\.dylib ",
    # HIL-bench infrastructure files — agent must not modify the judge scripts
    r"diff --git a/parser\.py b/parser\.py",
    r"diff --git a/run_script\.sh b/run_script\.sh",
    # Redis persistence files
    r"appendonlydir/",
    r"diff --git a/\S*dump\.rdb ",
    r"diff --git a/\S*appendonly\.aof ",
]
_PATCH_FILTER_RE = re.compile("|".join(_PATCH_FILTER_PATTERNS))


def filter_patch(patch: str) -> str:
    """Filter generated/binary/infrastructure files from an agent patch.

    Mirrors custom_eval.py filter_patch() exactly:
    - Removes __pycache__, .pyc, .so etc. to prevent git apply failures
    - Removes parser.py / run_script.sh changes to prevent test-infrastructure cheating
    - Removes Redis persistence files

    Called on the agent patch before git apply, matching _evaluate_single_instance() L2182.
    """
    if not patch:
        return patch
    file_diffs = re.split(r"(?=diff --git )", patch)
    filtered = []
    for diff in file_diffs:
        if not diff.strip():
            continue
        if _PATCH_FILTER_RE.search(diff):
            continue
        filtered.append(diff)
    return "".join(filtered)



def _build_sweap_cmd(tests_to_pass: list[str], run_script_content: str | None = None) -> str:
    """Build the SWEAP test command, passing FAIL_TO_PASS identifiers as args to run_script.sh.

    Mirrors custom_eval.py's augment_test_spec_with_required_tests logic:
    - Ansible (ansible-test): strips "path/file.py::Class::method" to just the file path.
      ansible-test does NOT understand pytest's :: notation; passing it causes the test class
      to be silently excluded. Canonical fix: strip to file path only (custom_eval.py L319-327).
    - JS/TS (pipe format): strips "file | description" to just the file path.
      run_script.sh implementations accept file paths; parser.py regenerates full names.
    - Go: pass function names as-is (used by run_script.sh with go test -run).
    - Other Python (pytest): pass full pytest IDs (path::Class::method).

    Without arguments, run_script.sh runs the entire test suite which is correct but slow.
    With arguments, it runs only the required tests, matching the canonical evaluation approach.
    """
    if not tests_to_pass:
        return SWEAP_TEST_CMD

    # Ansible-test special case: ansible-test does NOT understand pytest ::Class::method syntax.
    # If the run_script.sh uses ansible-test and args contain ::, strip to file paths only.
    # Mirrors custom_eval.py augment_test_spec_with_required_tests lines 319-327.
    uses_ansible_test = (
        run_script_content is not None
        and "ansible-test" in run_script_content
        and any("::" in t for t in tests_to_pass)
    )
    if uses_ansible_test:
        # Strip ::Class::method, deduplicate file paths
        seen: set[str] = set()
        args: list[str] = []
        for t in tests_to_pass:
            fp = t.split("::")[0] if "::" in t else t
            if fp not in seen:
                seen.add(fp)
                args.append(fp)
    else:
        # For JS/TS tests using "file | description" format, strip to file path only.
        # run_script.sh implementations (NodeBB, Protonmail, element-hq, etc.) accept file paths;
        # parser.py regenerates the full "file | description" names for matching.
        def _to_script_arg(t: str) -> str:
            if " | " in t:
                return t.split(" | ", 1)[0].strip()
            return t

        seen = set()
        args = []
        for raw in tests_to_pass:
            arg = _to_script_arg(raw)
            if arg not in seen:
                seen.add(arg)
                args.append(arg)

    # Shell-quote each argument (handle single quotes inside test names)
    def _sh_quote(s: str) -> str:
        return "'" + s.replace("'", "'\\''") + "'"

    quoted = " ".join(_sh_quote(a) for a in args)
    # Insert args after "run_script.sh" but before the redirect
    return SWEAP_TEST_CMD.replace("/root/run_script.sh", f"/root/run_script.sh {quoted}")

_print_lock = threading.Lock()


def log(msg: str, file=sys.stdout) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    with _print_lock:
        print(f"[{ts}] {msg}", file=file, flush=True)


# ── Per-uid owner tokens (mirrors run_hil_swe.py / paper_pipeline.py) ────────
#
# Token filename: "{uid}__{pid}__{uuid}.owner"
# eval_attempt() registers a token for its uid at entry and unregisters it in a
# finally block.  cleanup_orphaned_eval_containers() probes the PID before
# removing any running eval container, so a concurrent eval for the same uid is
# never killed mid-run.  Tokens are shared with run_hil_swe.py (same dir +
# format) so a live solve also guards against eval-container removal.

def _register_uid_owner(uid: str) -> Path:
    """Write a per-uid PID token; returns path for later unregistration."""
    RUN_OWNER_DIR.mkdir(parents=True, exist_ok=True)
    token = RUN_OWNER_DIR / f"{uid}__{os.getpid()}__{uuid.uuid4().hex}.owner"
    token.write_text(str(os.getpid()))
    return token


def _unregister_uid_owner(token: "Path | None") -> None:
    if not token:
        return
    token.unlink(missing_ok=True)


def _uid_has_live_owner(uid: str) -> bool:
    """True when any registered owner process for this uid is still alive.

    Mirrors run_hil_swe._uid_has_live_owner exactly: probe PID with kill(0),
    delete stale tokens for dead processes, be conservative on PermissionError.
    """
    if not RUN_OWNER_DIR.exists():
        return False
    for marker in RUN_OWNER_DIR.glob(f"{uid}__*__*.owner"):
        parts = marker.name.split("__")
        if len(parts) < 3:
            marker.unlink(missing_ok=True)
            continue
        try:
            pid = int(parts[1])
        except Exception:
            marker.unlink(missing_ok=True)
            continue
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            marker.unlink(missing_ok=True)
        except PermissionError:
            return True
    return False


def cleanup_orphaned_eval_containers(uid: str) -> int:
    """Remove orphaned eval containers for one uid.

    Uses the container name prefix th-eval-{uid[:12]}- (no ancestor filter
    needed since eval containers use the base hilbench-swe image, not the
    harness image, and base images vary per task).

    Mirrors cleanup_orphaned_containers in run_hil_swe.py:
    - Exited containers: always remove.
    - Running containers: only remove if _uid_has_live_owner(uid) is False.
    """
    _FMT = "{{.ID}}\t{{.Image}}\t{{.Names}}\t{{.Status}}\t{{.RunningFor}}"
    container_name_prefix = f"th-eval-{uid[:12]}-"
    try:
        by_name = subprocess.run(
            ["docker", "ps", "-a", "--format", _FMT,
             "--filter", f"name={container_name_prefix}"],
            capture_output=True, text=True, check=False,
        )
        to_remove: set[str] = set()
        for line in by_name.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 5:
                continue
            cid, status = parts[0], parts[3].lower()
            if status.startswith("exited"):
                to_remove.add(cid)
            elif status.startswith("up"):
                if not _uid_has_live_owner(uid):
                    to_remove.add(cid)
        if not to_remove:
            return 0
        subprocess.run(
            ["docker", "rm", "-f", *sorted(to_remove)],
            capture_output=True, check=False,
        )
        return len(to_remove)
    except Exception:
        return 0


def inspect_container_state(container_name: str) -> dict:
    """Return docker inspect .State for a container name, or {} on failure."""
    try:
        res = subprocess.run(
            ["docker", "inspect", "--format", "{{json .State}}", container_name],
            capture_output=True, text=True, check=False,
        )
        if res.returncode != 0:
            return {}
        txt = (res.stdout or "").strip()
        return json.loads(txt) if txt else {}
    except Exception:
        return {}


# ── SWEAP output parsing ────────────────────────────────────────────────────

def parse_sweap_json(output: str) -> list[dict]:
    """Extract and parse the SWEAP_JSON block from combined container output.

    Returns list of {name: str, status: str} dicts, or [] on parse failure.

    Mirrors custom_eval.py parse_log_sweap_json parsing strategies exactly:
      Strategy 1: Look for SWEAP_JSON_START/END markers (most reliable).
      Strategy 2: Look for {"tests" or {\\n "tests" pattern directly in output.
      Strategy 3: Try parsing the entire output as JSON (last resort).
    """
    data = None

    # Strategy 1: Markers — most reliable
    start = output.find(SWEAP_JSON_START)
    end = output.find(SWEAP_JSON_END)
    if start != -1 and end != -1 and end > start:
        json_str = output[start + len(SWEAP_JSON_START):end].strip()
        try:
            data = json.loads(json_str)
        except Exception:
            pass

    # Strategy 2: Look for JSON structure directly (fallback for truncated/interleaved output)
    if data is None:
        json_start = output.find('{\n "tests"')
        if json_start == -1:
            json_start = output.find('{"tests"')
        if json_start != -1:
            section = output[json_start:]
            for end_pattern in ['\n ]\n}', ']\n}', ']}']:
                end_pos = section.rfind(end_pattern)
                if end_pos != -1:
                    json_str = section[:end_pos + len(end_pattern)]
                    try:
                        data = json.loads(json_str)
                        break
                    except Exception:
                        continue

    # Strategy 3: Try parsing entire output (last resort)
    if data is None:
        try:
            data = json.loads(output.strip())
        except Exception:
            pass

    if data is None:
        return []
    return data.get("tests", [])


def _extract_pytest_components(test_name: str) -> tuple[str | None, str, str]:
    """Extract (file_path, func_with_params, func_base) from a pytest-style test name.

    Mirrors custom_eval.py's _extract_pytest_components exactly.
    Examples:
      "path/file.py::TestClass::test_foo[p1]" → ("path/file.py", "test_foo[p1]", "test_foo")
      "test_foo" → (None, "test_foo", "test_foo")
    """
    file_path = None
    func_with_params = test_name
    if "::" in test_name:
        parts = test_name.split("::")
        file_path = parts[0]
        func_with_params = parts[-1]
    func_base = func_with_params.split("[")[0] if "[" in func_with_params else func_with_params
    return file_path, func_with_params, func_base


def _paths_match(path1: str | None, path2: str | None) -> bool:
    """Check if two file paths match, handling different root prefixes.

    Mirrors custom_eval.py's _paths_match exactly.
    """
    if path1 is None and path2 is None:
        return True
    if path1 is None or path2 is None:
        return False
    if path1 == path2:
        return True
    return (
        path1.endswith("/" + path2)
        or path2.endswith("/" + path1)
        or path1.endswith(path2)
        or path2.endswith(path1)
    )


def _match_test_name(parser_name: str, required_tests: set[str]) -> str | None:
    """Find a required test name that matches parser_name, using fuzzy matching.

    Mirrors custom_eval.py's _find_matching_required_test exactly:
      1. Exact match
      2. JS/TS pipe format (file | description) with path/desc suffix matching
      3. Pytest format: path + func match with params_compatible check (two-pass:
         prefer path match, fall back to func-only)
    """
    # === 1. Exact match ===
    if parser_name in required_tests:
        return parser_name

    # === 2. JS/TS pipe format ===
    if " | " in parser_name:
        parser_path, parser_desc = parser_name.split(" | ", 1)
        for req in required_tests:
            if " | " in req:
                req_path, req_desc = req.split(" | ", 1)
                path_matches = (
                    req_path == parser_path
                    or req_path.endswith(parser_path)
                    or parser_path.endswith(req_path)
                )
                desc_matches = (
                    req_desc == parser_desc
                    or req_desc.endswith(" | " + parser_desc)
                    or parser_desc.endswith(" | " + req_desc)
                )
                if path_matches and desc_matches:
                    return req
            else:
                if (
                    req == parser_path
                    or req.endswith(parser_path)
                    or parser_path.endswith(req)
                ):
                    return req
        return None

    # === 3. Pytest format (path::func or just func, with or without params) ===
    parser_path, parser_func_params, parser_func_base = _extract_pytest_components(parser_name)
    parser_func_base_lower = parser_func_base.lower()
    fallback_match: str | None = None
    for req in required_tests:
        if " | " in req:
            continue
        req_path, req_func_params, req_func_base = _extract_pytest_components(req)
        if req_func_base.lower() != parser_func_base_lower:
            continue
        # Check parameter compatibility (mirrors canonical params_compatible exactly)
        params_compatible = (
            parser_func_params == req_func_params          # exact match
            or req_func_params == req_func_base            # required has no params (bare)
            or parser_func_params == parser_func_base      # parser has no params
        )
        if not params_compatible:
            continue
        # Prefer path matches; fall back to func-only (canonical two-pass strategy)
        if parser_path is not None and req_path is not None:
            if _paths_match(parser_path, req_path):
                return req                                  # best match: paths align
            if fallback_match is None:
                fallback_match = req
        else:
            return req                                      # at least one side has no path

    return fallback_match


def compute_resolved(tests: list[dict], tests_to_pass: list[str]) -> tuple[bool, list[str], list[str]]:
    """Return (resolved, passed_tests, failed_tests) given parsed test results.

    resolved = True iff every test in tests_to_pass has status PASSED.

    Mirrors custom_eval.py's parse_log_sweap_json scoring logic:
      1. Fuzzy test-name matching via _match_test_name (_find_matching_required_test)
      2. Parametrized test promotion: if FAIL_TO_PASS has a bare "test_foo" (no params)
         and all parametrized variants "test_foo[p1]", "test_foo[p2]" in the parser output
         PASSED, mark the bare required test as PASSED.  This mirrors custom_eval.py L736-778.
    """
    if not tests_to_pass:
        return False, [], []

    required_set = set(tests_to_pass)

    # Build status map: required test name → PASSED|FAILED|... (via fuzzy matching).
    # Mirrors canonical parse_log_sweap_json: later results overwrite earlier ones
    # (no first-match guard), so a bare required test ends up with the status of the
    # last parametrized variant that matched it.
    status_for_required: dict[str, str] = {}
    # Also keep a full parser-name → status map for the parametrized promotion step.
    parser_status: dict[str, str] = {}
    for t in tests:
        name = t.get("name", "")
        if not name:
            continue
        status = t.get("status", "MISSING")
        parser_status[name] = status
        matched = _match_test_name(name, required_set)
        if matched is not None:
            status_for_required[matched] = status  # overwrite — canonical behaviour

    # Parametrized test promotion (mirrors custom_eval.py L736-778):
    # If a required test is bare (no "["), and all parametrized variants of that
    # test found in parser_status are PASSED, promote the bare required test to PASSED.
    for req in required_set:
        if req in status_for_required:
            continue                                 # already matched
        if " | " in req or "[" in req:
            continue                                 # skip JS/TS pipe and already-parametrized
        req_path, _, req_func_base = _extract_pytest_components(req)
        req_func_base_lower = req_func_base.lower()
        parametrized_variants: list[str] = []
        for pname, pstatus in parser_status.items():
            if "[" not in pname:
                continue                             # only look at parametrized parser names
            st_path, _, st_func_base = _extract_pytest_components(pname)
            if st_func_base.lower() != req_func_base_lower:
                continue
            if req_path is not None:
                if st_path is None or not _paths_match(req_path, st_path):
                    continue
            parametrized_variants.append(pname)
        if parametrized_variants and all(parser_status.get(v) == "PASSED" for v in parametrized_variants):
            status_for_required[req] = "PASSED"

    passed = [t for t in tests_to_pass if status_for_required.get(t, "MISSING") == "PASSED"]
    failed = [t for t in tests_to_pass if status_for_required.get(t, "MISSING") != "PASSED"]
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
    infra_retries: int,
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

    # Read run_script.sh to detect ansible-test (needed by _build_sweap_cmd).
    # Mirrors custom_eval.py evaluate_from_metadata which reads run_script_content.
    run_script_path = task_dir / "run_script.sh"
    run_script_content: str | None = run_script_path.read_text() if run_script_path.exists() else None

    label = f"[{uid[:12]}|{mode}|p{pass_index}]"

    # Apply canonical filter_patch before writing to temp file.
    # Mirrors _evaluate_single_instance() L2182 in custom_eval.py:
    #   clean_patch = filter_patch(raw_patch)
    # Strips __pycache__, .pyc, node_modules, parser.py, run_script.sh etc.
    raw_agent_patch = patch_path.read_text()
    clean_agent_patch = filter_patch(raw_agent_patch)
    if raw_agent_patch != clean_agent_patch:
        log(f"{label} Filtered generated/infrastructure files from agent patch")

    # Register a per-uid owner token so cleanup_orphaned_eval_containers knows
    # this eval is active.  Mirrors run_hil_swe.run_attempt: non-fatal, always
    # unregistered in a finally block.
    owner_token: Path | None = None
    try:
        owner_token = _register_uid_owner(uid)
    except Exception as exc:
        log(f"{label} WARNING: failed to register eval owner token: {exc}", file=sys.stderr)

    # Clean up any orphaned eval containers from previous crashed runs for this uid.
    cleanup_orphaned_eval_containers(uid)

    # Extract test files from the test patch for the reset command.
    # Strip \r from the header lines first (safe: headers never need \r) so that
    # the regex reliably captures paths even when the patch has CRLF headers.
    # We do NOT blindly strip \r from content lines here — some fixture files
    # (e.g. MIME multipart .txt) are genuinely CRLF in the repo and their patch
    # content lines must stay CRLF.  Full normalization happens inside the container
    # via normalize_patch.py which reads each target file to decide per-file.
    _HEADER_STARTS = (
        "diff --git ", "--- ", "+++ ", "index ", "@@ ",
        "old mode ", "new mode ", "deleted file mode ", "new file mode ",
        "similarity index ", "rename from ", "rename to ",
    )
    _header_stripped_lines = [
        line.rstrip("\r") if any(line.startswith(h) for h in _HEADER_STARTS) else line
        for line in test_patch.split("\n")
    ]
    test_patch_for_header_scan = "\n".join(_header_stripped_lines)

    # Mirrors swebench's make_eval_script_list:
    #   DIFF_MODIFIED_FILE_REGEX = r"--- a/(.*)"
    #   reset_tests_command = f"git checkout {base_commit} {' '.join(test_files)}"
    _raw_test_files = re.findall(r"--- a/(.*)", test_patch_for_header_scan)
    _test_files = [f.strip() for f in _raw_test_files if f.strip() and f.strip() != "/dev/null"]

    def _sh_quote_path(s: str) -> str:
        return "'" + s.replace("'", "'\\''") + "'"

    if _test_files:
        reset_test_files_cmd = (
            "git checkout HEAD -- " + " ".join(_sh_quote_path(f) for f in _test_files)
        )
    else:
        reset_test_files_cmd = "# no test files extracted from test patch"

    # Write patches and the in-container normalizer to temp files for bind-mounting.
    with tempfile.TemporaryDirectory(prefix=f"th_eval_{uid[:8]}_") as tmpdir:
        tmp = Path(tmpdir)
        agent_patch_file = tmp / "agent.patch"
        test_patch_file = tmp / "test.patch"
        normalizer_file = tmp / "normalize_patch.py"

        agent_patch_file.write_text(clean_agent_patch)
        # Write the raw test patch (preserving original \r in content lines).
        # The in-container normalizer handles per-file CRLF normalization.
        test_patch_file.write_bytes(test_patch.encode("utf-8"))
        # normalize_patch.py: direct port of hil_bench_agent.normalize_golden_script
        # (lines 6565-6611 of hil_bench_agent.py).  That script is itself a
        # serialisation of _normalize_patch_line_endings (line 780).
        # Differences from the original that are forced by our context:
        #   - reads patch_in (sys.argv[1]) and writes patch_out (sys.argv[3])
        #     instead of modifying the file in-place, because /tmp/test.patch is
        #     mounted read-only.
        #   - repo_dir taken from sys.argv[2] (/app).
        # Everything else — the 5 header prefixes, the append+continue pattern,
        # the file-CRLF detection via rb read(8192), the content-line normalization
        # — is identical to the canonical script.
        normalizer_file.write_text(
            r"""import os, sys
patch_in, repo_dir, patch_out = sys.argv[1], sys.argv[2], sys.argv[3]
with open(patch_in, 'r', encoding='utf-8', errors='replace') as f:
    patch_content = f.read()
if not patch_content.strip():
    open(patch_out, 'w').close()
    sys.exit(0)
lines = patch_content.split('\n')
result_lines = []
current_file = None
file_has_crlf = {}
for line in lines:
    if line.startswith('diff --git ') or line.startswith('--- ') or line.startswith('+++ ') or line.startswith('index ') or line.startswith('@@ '):
        line = line.rstrip('\r')
    if line.startswith('diff --git '):
        parts = line.split()
        if len(parts) >= 4:
            file_path = parts[3].rstrip('\r')
            if file_path.startswith('b/'):
                file_path = file_path[2:]
            current_file = file_path
            target_path = os.path.join(repo_dir, file_path)
            if os.path.exists(target_path):
                try:
                    with open(target_path, 'rb') as f:
                        content = f.read(8192)
                    file_has_crlf[file_path] = b'\r\n' in content
                except Exception:
                    file_has_crlf[file_path] = False
            else:
                file_has_crlf[file_path] = False
        result_lines.append(line)
        continue
    if current_file is not None:
        if line.startswith((' ', '+', '-')):
            if file_has_crlf.get(current_file, False):
                if not line.endswith('\r'):
                    line = line + '\r'
            else:
                if line.endswith('\r'):
                    line = line[:-1]
    result_lines.append(line)
normalized = '\n'.join(result_lines)
with open(patch_out, 'w', encoding='utf-8') as f:
    f.write(normalized)
"""
        )

        # Eval script: runs inside the container.
        # Mirrors swebench run_evaluation.py + make_eval_script_list exactly:
        #
        # 1. Setup (git config) — mirrors make_repo_script_list_local() / make_eval_script_list
        # 2. Apply agent patch; fall back to patch --fuzz=5
        #    Mirrors run_evaluation.py L122-146:
        #      git apply --allow-empty -v /tmp/patch.diff
        #      OR patch --batch --fuzz=5 -p1 -i /tmp/patch.diff
        #      BOTH fail → EvaluationError → exit 3 (unresolved, no test run)
        # 3. Reset test files to HEAD — mirrors reset_tests_command in make_eval_script_list
        #      git checkout HEAD -- {test_files}
        # 4. Normalize test patch line endings per target file (mirrors
        #    hil_bench_agent._normalize_patch_line_endings): python3 reads each file
        #    in /app to detect CRLF vs LF, normalizes patch content lines to match.
        # 5. Apply normalized test patch
        # 6. Run SWEAP_TEST_CMD
        eval_script = (
            r"""#!/bin/sh
set -e
cd /app

# Setup mirrors custom_eval.py make_repo_script_list_local() / swebench make_eval_script_list.
# NOTE: chmod -R 777 /app is intentionally omitted. hilbench-swe containers run as root,
# which already has full access to all files regardless of permissions. The recursive
# chmod on large repos causes multi-minute I/O stalls with no benefit.
git config --global user.email setup@swebench.config
git config --global user.name SWE-bench
# Mirrors swebench make_eval_script_list: safe.directory for nonroot users
git config --global --add safe.directory /app

# Apply agent patch.
# Mirrors swebench run_evaluation.py exactly:
#   git apply --allow-empty -v /tmp/patch.diff
#   fallback: patch --batch --fuzz=5 -p1 -i /tmp/patch.diff
# If both fail → APPLY_PATCH_FAIL (here: exit 3 => unresolved).
if git apply --allow-empty -v /tmp/agent.patch 2>/tmp/agent_patch.log; then
  echo "PATCH_APPLY_STATUS: ok"
else
  echo "git apply failed, trying patch --batch --fuzz=5..." >&2
  cat /tmp/agent_patch.log >&2
  if patch --batch --fuzz=5 -p1 -i /tmp/agent.patch 2>/tmp/agent_patch2.log; then
    echo "PATCH_APPLY_STATUS: ok (patch fallback)"
  else
    echo "PATCH_APPLY_STATUS: failed"
    cat /tmp/agent_patch2.log >&2
    exit 3
  fi
fi

# Reset test files to HEAD before applying the test patch.
# Mirrors swebench make_eval_script_list reset_tests_command:
#   git checkout {base_commit} {test_files}
# If the agent modified test files, this undoes those changes so the test
# patch can be applied cleanly. base_commit is HEAD for hilbench-swe images.
"""
            + reset_test_files_cmd
            + r"""

# Normalize test patch line endings against actual files in /app.
# Mirrors hil_bench_agent._normalize_patch_line_endings: always strip \r from
# header lines; for content lines, add or strip \r to match the file's endings.
# This handles patches where some files are CRLF (e.g. MIME fixtures) and
# others are LF. A simple blanket replace("\r\n","\n") would break CRLF files.
if command -v python3 >/dev/null 2>&1; then
  python3 /tmp/normalize_patch.py /tmp/test.patch /app /tmp/test_normalized.patch
else
  # Fallback for images without Python: strip all \r (works for LF-only repos).
  tr -d '\r' < /tmp/test.patch > /tmp/test_normalized.patch
fi

if git apply -v /tmp/test_normalized.patch 2>/tmp/test_patch.log; then
  echo "TEST_PATCH_STATUS: ok"
else
  echo "TEST_PATCH_STATUS: failed"
  cat /tmp/test_patch.log >&2
  exit 2
fi

# Turn off exit-on-error for the test run: run_script.sh exits non-zero when
# tests fail, which is the expected case for unsolved attempts.  We still need
# parser.py to capture the per-test results, so we cannot let set -e bail here.
set +e
"""
            + _build_sweap_cmd(tests_to_pass, run_script_content)
        )
        eval_script_file = tmp / "eval.sh"
        eval_script_file.write_text(eval_script)

        # Unique name so we can kill by name on timeout (same pattern as run_hil_swe.py).
        # Format: th-eval-<uid12>-<mode>-p<pass>-r<run_id_hash12>
        container_name = f"th-eval-{uid[:12]}-{mode}-p{pass_index}-r{_run_id_token(run_id)}"

        cmd = [
            "docker", "run",
            "--name", container_name,
            # hilbench-swe base images have ENTRYPOINT ["sleep", "infinity"] baked in.
            # The harness image clears this with ENTRYPOINT [] in Dockerfile.harness, but
            # the eval container uses the raw base image, so we MUST override it here.
            # This mirrors ask_config_claude_opus_4-6.yaml: docker_args: ["--entrypoint=", ...]
            "--entrypoint", "",
            # hilbench-swe images have pip.conf pointing to non-existent 127.0.0.1:9876
            # (same fix as ask_config_claude_opus_4-6.yaml and _DOCKERFILE_INSTANCE_PRECONFIGURED)
            "-e", "PIP_INDEX_URL=https://pypi.org/simple/",
            # Prevent git/man/less from opening a pager (hangs in non-interactive containers).
            # Matches ask_config_claude_opus_4-6.yaml env_variables exactly.
            "-e", "GIT_PAGER=cat",
            "-e", "PAGER=cat",
            "-e", "MANPAGER=cat",
            "-e", "LESS=-R",
            "-e", "LANG=C.UTF-8",
            "-e", "LC_ALL=C.UTF-8",
            # bind-mount patches, normalizer, and eval script read-only
            "-v", f"{agent_patch_file}:/tmp/agent.patch:ro",
            "-v", f"{test_patch_file}:/tmp/test.patch:ro",
            "-v", f"{normalizer_file}:/tmp/normalize_patch.py:ro",
            "-v", f"{eval_script_file}:/tmp/eval.sh:ro",
            # No harness needed for eval — use the clean base image
            base_image,
            "sh", "/tmp/eval.sh",
        ]

        max_attempts = max(1, infra_retries + 1)
        stdout_data = b""
        stderr_data = b""
        elapsed = 0
        returncode: int | None = None
        state: dict = {}

        for infra_attempt in range(1, max_attempts + 1):
            log(f"{label} Starting eval container ({base_image}) [attempt {infra_attempt}/{max_attempts}]")
            started_at = time.time()
            proc: subprocess.Popen | None = None
            try:
                # Use Popen so we can explicitly kill the docker CLI process on timeout.
                # subprocess.run(timeout=...) raises TimeoutExpired but does NOT kill the child.
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    stdout_data, stderr_data = proc.communicate(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()  # drain and reap
                    subprocess.run(["docker", "rm", "-f", container_name],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                    err = f"eval timed out after {timeout_s}s"
                    log(f"{label} {err}", file=sys.stderr)
                    _write_eval_result(eval_path, uid, mode, pass_index, error=err)
                    _unregister_uid_owner(owner_token)
                    return False, f"{label} {err}"
            except Exception as exc:
                if proc is not None:
                    try:
                        proc.kill()
                        proc.communicate()
                    except Exception:
                        pass
                subprocess.run(["docker", "rm", "-f", container_name],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                err = str(exc)
                log(f"{label} Exception: {err}", file=sys.stderr)
                _write_eval_result(eval_path, uid, mode, pass_index, error=err)
                _unregister_uid_owner(owner_token)
                return False, f"{label} Exception: {err}"

            elapsed = int(time.time() - started_at)
            returncode = proc.returncode
            state = inspect_container_state(container_name)
            subprocess.run(["docker", "rm", "-f", container_name],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

            # Retry fast-fail infra kills (e.g., transient OOM/SIGKILL) before classification.
            # Only retry when no SWEAP JSON was produced, because valid test output should be
            # classified immediately (resolved/unresolved/infra_error) without mutation.
            combined_output = stdout_data.decode(errors="replace") + "\n" + stderr_data.decode(errors="replace")
            test_results_probe = parse_sweap_json(combined_output)
            oom_killed_probe = bool(state.get("OOMKilled")) if isinstance(state, dict) else False
            transient_kill = returncode == 137 and not test_results_probe
            if transient_kill and infra_attempt < max_attempts:
                reason = "OOMKilled" if oom_killed_probe else "SIGKILL/exit137"
                log(f"{label} transient eval infra kill ({reason}); retrying...", file=sys.stderr)
                time.sleep(min(5 * infra_attempt, 15))
                continue
            break

        combined_output = stdout_data.decode(errors="replace") + "\n" + stderr_data.decode(errors="replace")

        # Determine patch apply status
        patch_applied = "PATCH_APPLY_STATUS: ok" in combined_output
        assert returncode is not None
        # test_ran is True only when the test harness actually ran (exit 0 or 1).
        # exit 2  = test patch failed to apply (explicit `exit 2` in eval_script) → tests never ran.
        # exit 3  = agent patch failed to apply (both git apply and patch --fuzz=5 failed) → unresolved.
        # exit other (e.g. 137 SIGKILL, container crash) → tests never ran → infra_error.
        test_ran = returncode in (0, 1)

        # Parse SWEAP JSON
        test_results = parse_sweap_json(combined_output)
        resolved, passed_tests, failed_tests = compute_resolved(test_results, tests_to_pass)

        # ── Classify eval outcome ──────────────────────────────────────────────────
        # eval_status:
        #   "resolved"    – all FAIL_TO_PASS tests passed
        #   "unresolved"  – tests ran (or agent patch failed), not resolved
        #   "infra_error" – test patch itself failed to apply, or unexpected container exit
        #
        # exit 2 = test patch failed (`exit 2` in eval_script).
        # We reset test files to HEAD before applying the test patch (mirroring swebench's
        # reset_tests_command), so agent-caused test file modifications no longer trigger
        # exit 2. If the test patch still can't apply after the reset, it's a dataset issue.
        #
        # exit 3 = agent patch failed (both git apply and patch --fuzz=5 failed).
        # Mirrors canonical run_evaluation.py EvaluationError("APPLY_PATCH_FAIL"):
        # an agent's failing patch is NOT an infra error, it's an unresolved attempt
        # (the agent is at fault, not our infrastructure).
        if returncode == 3:
            # Agent patch failed — mirrors canonical EvaluationError("APPLY_PATCH_FAIL")
            eval_status = "unresolved"
            error_msg = f"agent patch failed to apply (APPLY_PATCH_FAIL); stderr: {stderr_data.decode(errors='replace')[:500]}"
        elif returncode == 2:
            eval_status = "infra_error"
            error_msg = f"container exited {returncode}; stderr: {stderr_data.decode(errors='replace')[:500]}"
        elif returncode not in (0, 1):
            eval_status = "infra_error"
            error_msg = f"container exited {returncode}; stderr: {stderr_data.decode(errors='replace')[:500]}"
        elif resolved:
            eval_status = "resolved"
            error_msg = None
        else:
            eval_status = "unresolved"
            error_msg = None

        oom_killed = bool(state.get("OOMKilled")) if isinstance(state, dict) else False
        eval_data = {
            "uid": uid,
            "mode": mode,
            "pass_index": pass_index,
            "eval_status": eval_status,
            "resolved": resolved,
            "patch_applied": patch_applied,
            "test_ran": test_ran,
            "tests_to_pass": tests_to_pass,
            "passed_tests": passed_tests,
            "failed_tests": failed_tests,
            "all_tests": test_results,
            "container_exit_code": returncode,
            "oom_killed": oom_killed,
            "elapsed_s": elapsed,
            "error": error_msg,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
        }
        eval_path.write_text(json.dumps(eval_data, indent=2))

        if eval_status == "resolved":
            status_str = "RESOLVED ✓"
        elif eval_status == "infra_error":
            status_str = f"infra_error (exit {returncode})"
        else:
            status_str = "unresolved"
        log(f"{label} {status_str} in {elapsed}s ({len(passed_tests)}/{len(tests_to_pass)} FAIL_TO_PASS tests)")
        _unregister_uid_owner(owner_token)
        return True, f"{label} {status_str}"


def _write_eval_result(path: Path, uid: str, mode: str, pass_index: int, error: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "uid": uid,
        "mode": mode,
        "pass_index": pass_index,
        "eval_status": "infra_error",
        "resolved": False,
        "patch_applied": False,
        "test_ran": False,
        "tests_to_pass": [],
        "passed_tests": [],
        "failed_tests": [],
        "all_tests": [],
        "container_exit_code": None,
        "oom_killed": False,
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
        "--modes", nargs="+", choices=["neutral", "skill", "full_info", "no_tool"],
        help="Restrict to these modes (default: all).",
    )
    parser.add_argument(
        "--passes", type=int, default=None,
        help="Only evaluate up to this pass number (default: all).",
    )
    parser.add_argument("--force", action="store_true", help="Re-evaluate even if eval_result.json exists.")
    parser.add_argument(
        "--workers", "-w", type=int, default=None,
        help="Max concurrent eval containers (default: min(num_jobs, 10)).",
    )
    parser.add_argument(
        "--timeout", type=int, default=3600,
        help="Per-attempt eval timeout in seconds (default: 3600).",
    )
    parser.add_argument(
        "--infra-retries", type=int, default=1,
        help="Retries for transient eval infra kills (exit 137 with no SWEAP JSON). Default: 1.",
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

    workers = args.workers if args.workers is not None else min(len(jobs), 10)
    log(f"Evaluating {len(jobs)} attempt(s) with {workers} worker(s) — run_id='{args.run_id}'")

    successes: list[str] = []
    failures: list[str] = []
    evaluated_uids: set[str] = set()

    try:
        def run_one(job: dict) -> tuple[bool, str]:
            evaluated_uids.add(job["uid"])
            return eval_attempt(
                uid=job["uid"],
                mode=job["mode"],
                pass_index=job["pass_index"],
                run_id=args.run_id,
                skip_if_complete=not args.force,
                timeout_s=args.timeout,
                infra_retries=max(0, args.infra_retries),
            )

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(run_one, j): j for j in jobs}
            for future in as_completed(futures):
                ok, msg = future.result()
                (successes if ok else failures).append(msg)
    finally:
        # Last-resort sweep: remove any eval containers that were left running
        # (e.g. if the process was SIGKILLed mid-communicate).
        cleaned = sum(cleanup_orphaned_eval_containers(uid) for uid in evaluated_uids)
        if cleaned:
            log(f"Cleaned up {cleaned} orphaned eval container(s)")

    log(f"\n{'='*60}")
    log(f"Done: {len(successes)} evaluated, {len(failures)} failed.")
    for msg in failures:
        log(f"  FAILED: {msg}", file=sys.stderr)
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
