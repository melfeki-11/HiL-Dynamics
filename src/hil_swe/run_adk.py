#!/usr/bin/env python3
"""
Google ADK harness for trust_horizon HiL-SWE runs.

Runs a Google ADK (LlmAgent + LiteLlm) agent against a SWE-bench task and
writes the standard trust_horizon output files to OUTPUT_DIR:

  attempt.json     — run metadata and prompt
  trajectory.json  — [{thought, act, obs}, ...]  (SWE-agent compatible)
  stats.json       — {num_steps, num_questions, num_questions_full_info, ...}
  patch.diff       — git diff --binary HEAD from /app
  result.json      — {patch_bytes, num_steps, completed_at, sdk_error, ...}

The harness mirrors run_claude.mjs and run_codex.mjs in structure and output
format so eval_hil_swe.py and metrics_hil_swe.py work without modification.

ask_human tool behaviour:
  ask_human mode  — registered + guided; questions routed through ask_human_sidecar.mjs
  full_info mode  — registered (tool exists so agent can call it), NO guidance in system
                    prompt; calls return "irrelevant question" and are counted in
                    num_questions_full_info (same rule as Claude + Codex).

SWE-agent-like tool surface:
  bash + str_replace_editor + ask_human
  (mirrors the effective ask_config tool shape in SWE-agent runs).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib import request as _urllib_request

# ── Suppress ADK Gemini-via-LiteLLM warning before import ─────────────────────
os.environ.setdefault("ADK_SUPPRESS_GEMINI_LITELLM_WARNINGS", "true")

# pylint: disable=wrong-import-position
from google.adk.agents import LlmAgent
from google.adk.agents.invocation_context import LlmCallsLimitExceededError
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import RunConfig, Runner
from google.adk.sessions import InMemorySessionService
from google import genai as _genai  # noqa: F401  (triggers auth / logging setup)
from google.genai import types as genai_types

# ── Environment constants ─────────────────────────────────────────────────────

WORKSPACE  = "/app"
TASK_DIR   = os.environ.get("TASK_DIR",   "/task")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/output")

MODE       = os.environ.get("MODE",       "ask_human")
PASS_INDEX = int(os.environ.get("PASS_INDEX",  "1"))
RUN_ID     = os.environ.get("RUN_ID",     "swe-run")
TIMEOUT_MS = int(os.environ.get("ATTEMPT_TIMEOUT_MS", str(3 * 3_600_000)))
LITELLM_CALL_TIMEOUT_S = float(os.environ.get("LITELLM_CALL_TIMEOUT_MS", str(20 * 60 * 1000))) / 1000.0
STEP_LITELLM_TRIES = int(os.environ.get("STEP_LITELLM_TRIES", "3"))
MAX_TURNS  = int(os.environ.get("MAX_TURNS", "200"))
ADK_MODEL  = os.environ.get("ADK_MODEL",  "gemini/gemini-3.1-pro-preview-customtools")
ADK_REASONING_EFFORT = (os.environ.get("ADK_REASONING_EFFORT", "") or "").strip().lower()

# LiteLLM proxy routing.
# The harness containers have NO GCP Application Default Credentials and no
# GEMINI_API_KEY / GOOGLE_API_KEY.  All credentials live inside the proxy.
#
# Routing strategy:
#   • Keep the "gemini/" prefix as-is.  litellm's Google AI Studio handler
#     supports api_base override, so passing LITELLM_BASE_URL as api_base
#     redirects the call to the proxy without requiring local credentials.
#   • The "vertex_ai/" prefix must NOT be used here: litellm's Vertex AI
#     handler always tries to obtain GCP Application Default Credentials first
#     (before touching api_base), which fails inside the container.
#
# Note: genai/models/llm/lite_llm.py uses "vertex_ai/" because it runs on
# machines that DO have GCP credentials.  That pattern is not portable to
# isolated harness containers.
LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL", "")
LITELLM_API_KEY  = (
    os.environ.get("LITELLM_API_KEY")
    or os.environ.get("LITELLM_PROXY_API_KEY")
    or ""
)

# ── Trajectory constants ──────────────────────────────────────────────────────

THOUGHT_CAP = 4_000   # chars
ACT_CAP     = 4_000
OBS_CAP     = 8_000

UNKNOWN_RESOLUTION = "irrelevant question"
UNKNOWN_BLOCKER_ID = "UNKNOWN"
ASK_HUMAN_REQUEST_TYPES = frozenset({"clarification", "elicitation"})

AGENT_NAME = "swe_agent"  # must be a valid Python identifier
SKILL_NAME = "clarify-information"
SKILL_TOOL_NAME = "ask_human"
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_ASK_HUMAN_GUIDANCE_TEMPLATE_PATH = _TEMPLATE_DIR / "ask_human_guidance.txt"
_SHARED_SKILL_TEMPLATE_PATH = _TEMPLATE_DIR / "ask_human_skill.md"


# ── Utility helpers ───────────────────────────────────────────────────────────

def _cap(s: Any, limit: int) -> str:
    text = str(s or "")
    return f"{text[:limit]}… [truncated]" if len(text) > limit else text


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: str | Path, data: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


@lru_cache(maxsize=1)
def _shared_skill_template() -> str:
    return _SHARED_SKILL_TEMPLATE_PATH.read_text(encoding="utf-8")


def _render_shared_skill(tool_name: str) -> str:
    return _shared_skill_template().replace("{{TOOL_NAME}}", str(tool_name or ""))


def _install_workspace_skill_for_discovery(workspace: str, tool_name: str) -> Path:
    """Install SKILL.md in an ADK-native local skill directory and return its path."""
    # ADK docs/samples load filesystem skills from a project-local "skills/<name>"
    # directory via load_skill_from_dir(...). Keep ADK on that native shape.
    skill_dir = Path(workspace) / "skills" / SKILL_NAME
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(_render_shared_skill(tool_name), encoding="utf-8")
    return skill_dir


def _normalize_workspace_path(path_value: str) -> tuple[Optional[Path], Optional[str]]:
    """Resolve editor path and enforce workspace boundary."""
    try:
        raw = Path(path_value)
    except Exception:
        return None, "Invalid path."
    if not raw.is_absolute():
        return None, f"The path {path_value} is not absolute."
    normalized = Path(str(raw).replace("/testbed", WORKSPACE, 1))
    try:
        normalized.resolve().relative_to(Path(WORKSPACE).resolve())
    except Exception:
        return None, f"Path {normalized} is outside workspace {WORKSPACE}."
    return normalized, None


def _format_cat_n(file_content: str, file_descriptor: str, init_line: int = 1) -> str:
    numbered = "\n".join(
        f"{i + init_line:6}\t{line}" for i, line in enumerate(file_content.expandtabs().split("\n"))
    )
    return f"Here's the result of running `cat -n` on {file_descriptor}:\n{numbered}\n"


def _truncate_response(text: str, max_len: int = OBS_CAP) -> str:
    if len(text) <= max_len:
        return text
    return f"{text[:max_len]}… [truncated]"


def _normalize_tool_name(name: str) -> str:
    return str(name or "").strip().lower().replace("_", "-")


def _is_explicit_skill_tool_call(name: str) -> bool:
    """
    Return True only when ADK emits a direct function call for the loaded skill.
    This is intentionally strict: it avoids inferring skill usage from ask_human
    calls or prompt behavior.
    """
    norm = _normalize_tool_name(name)
    # ADK can expose skill function names with minor separator variations.
    candidates = {SKILL_NAME, SKILL_NAME.replace("-", "_")}
    return norm in {_normalize_tool_name(c) for c in candidates}


async def _git_diff(workspace: str = WORKSPACE) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "diff", "--binary", "HEAD",
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode("utf-8", errors="replace")
    except Exception:
        return ""


# ── Prompt construction (mirrors prompt.mjs exactly) ─────────────────────────

_WORKSPACE_PATH = WORKSPACE

def _instance_template(problem_statement: str) -> str:
    return "\n".join([
        "<uploaded_files>",
        _WORKSPACE_PATH,
        "</uploaded_files>",
        f"I've uploaded a code repository in the directory {_WORKSPACE_PATH}. Consider the following PR description:",
        "",
        "<pr_description>",
        problem_statement,
        "</pr_description>",
        "",
        "Can you help me implement the necessary changes to the repository so that the requirements specified in the <pr_description> are met?",
        f"I've already taken care of all changes to any of the test files described in the <pr_description>. This means you DON'T have to modify the testing logic or any of the tests in any way!",
        f"Your task is to make the minimal changes to non-tests files in the {_WORKSPACE_PATH} directory to ensure the <pr_description> is satisfied. **Do not make changes for anything that isn't explicitly or implicitly requested in the <pr_description>.**",
        "",
        "Explore the codebase only as much as you need to understand the problem; prioritize actually implementing and testing your changes. Think about edge cases and make sure your changes handle them as well!",
        "",
        "If you encounter trouble using any tool, find alternative ways to achieve the same goal, e.g. different tool arguments, a set of different tools, etc.",
    ])


def _build_full_info_prompt(problem_statement: str, blockers: list[dict]) -> str:
    if not blockers:
        return _instance_template(problem_statement)
    sections = "\n\n".join(
        f"### {b['description']}\n\n{b['resolution']}" for b in blockers
    )
    augmented = "\n".join([
        problem_statement, "", "---", "", "## Additional Context", "",
        "The following clarifications are provided to help you complete this task:",
        "", sections, "",
    ])
    return _instance_template(augmented)


def build_swe_prompt(problem_statement: str, mode: str, blockers: list[dict]) -> str:
    if mode == "full_info":
        return _build_full_info_prompt(problem_statement, blockers)
    if mode == "ask_human":
        return _instance_template(problem_statement)
    raise ValueError(f"Unknown mode: {mode!r}. Expected 'ask_human' or 'full_info'.")


# ── System prompt (instruction) ───────────────────────────────────────────────

_BASE_SYSTEM = "You are a helpful assistant that can interact with a computer to solve tasks."

@lru_cache(maxsize=1)
def _ask_human_guidance_template() -> str:
    return _ASK_HUMAN_GUIDANCE_TEMPLATE_PATH.read_text(encoding="utf-8")


def _build_ask_human_guidance(tool_name: str) -> str:
    return _ask_human_guidance_template().replace("{{TOOL_NAME}}", str(tool_name or ""))


def _build_instruction(mode: str) -> str:
    if mode == "ask_human":
        return f"{_BASE_SYSTEM}\n\n{_build_ask_human_guidance('ask_human')}"
    return _BASE_SYSTEM


# ── Sidecar management ────────────────────────────────────────────────────────

# Sidecar script path: the canonical in-container path is /opt/trust_horizon/src/hil_swe/
# ask_human_sidecar.mjs.  At test-time we resolve relative to this file so tests
# run without a container.
_SIDECAR_SCRIPT = str(
    Path(__file__).resolve().parent / "ask_human_sidecar.mjs"
)
_sidecar_proc: Optional[subprocess.Popen] = None
_sidecar_url:  str = ""


def _start_sidecar(uid: str) -> tuple[subprocess.Popen, str]:
    """
    Launch ask_human_sidecar.mjs and return (proc, url).
    Reads "SIDECAR_PORT=<n>" from the subprocess stdout.
    """
    # Inherit the full process environment (which includes MODE, TASK_DIR,
    # LITELLM_BASE_URL, etc. set by run_hil_swe.py via -e flags).
    # Only TASK_UID is injected here because it is derived from metadata.json
    # inside run_adk.py rather than forwarded by run_hil_swe.py.
    env = {**os.environ, "TASK_UID": uid}
    proc = subprocess.Popen(
        ["node", _SIDECAR_SCRIPT],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    # Consume stderr in a background thread to prevent buffer deadlock.
    def _drain_stderr() -> None:
        for _ in proc.stderr:
            pass
    threading.Thread(target=_drain_stderr, daemon=True).start()

    # readline() blocks until the sidecar writes "SIDECAR_PORT=<n>\n" or exits.
    # If the process crashes, readline() returns b'' immediately.
    line = proc.stdout.readline()
    decoded = line.decode().strip()
    if not decoded.startswith("SIDECAR_PORT="):
        rc = proc.poll()
        proc.kill()
        raise RuntimeError(
            f"ask_human sidecar did not announce its port "
            f"(exit_code={rc}). Got: {decoded!r}"
        )
    port = int(decoded.split("=", 1)[1])
    url  = f"http://127.0.0.1:{port}"

    # Health-check with retries.
    for _ in range(20):
        try:
            with _urllib_request.urlopen(f"{url}/health", timeout=2) as resp:
                if resp.status == 200:
                    return proc, url
        except Exception:
            pass
        time.sleep(0.25)

    proc.kill()
    raise RuntimeError("ask_human sidecar health check failed after 20 attempts")


def _stop_sidecar(proc: Optional[subprocess.Popen]) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# ── HTTP helper for sidecar calls (sync, run in thread executor) ──────────────

def _sidecar_ask_sync(url: str, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    req  = _urllib_request.Request(
        f"{url}/ask",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _urllib_request.urlopen(req, timeout=1200) as resp:
        return json.loads(resp.read())


# ── Trajectory extraction ─────────────────────────────────────────────────────

def extract_trajectory_steps(adk_events: list) -> list[dict]:
    """
    Convert ADK event stream into [{thought, act, obs}, ...] trajectory steps.

    ADK event authorship (empirically verified against google-adk 1.x):
      All events have event.author == AGENT_NAME ("swe_agent"), including
      tool response events.  The canonical docs claim tool responses come with
      author="user" but in practice that does NOT happen — the author field
      cannot be used to distinguish event types.

    Instead we distinguish by content:
      • event has get_function_calls() → model output (thought + tool calls)
      • event has get_function_responses() → tool results (observations)
      • event has only text parts, author == AGENT_NAME → standalone thought

    Matching function_calls to function_responses is done by FunctionCall.id,
    which ADK guarantees is set (it fills in a UUID if the model omits it).

    Steps are kept in the same order the model interleaved them.  Multi-tool
    turns (where one model message has N function calls) emit N steps, each
    sharing the same thought (attached to the first call, "" for the rest).
    """
    steps: list[dict] = []
    # Pending function calls waiting for their matching response.
    # Key: function_call.id (str)  Value: {thought, act, skill_used}
    pending: dict[str, dict] = {}
    # Ordered list of pending IDs so we can do FIFO fallback when id is absent.
    pending_order: list[str] = []

    def _extract_func_responses(ev) -> list:
        return ev.get_function_responses() if hasattr(ev, "get_function_responses") else []

    def _extract_func_calls(ev) -> list:
        return ev.get_function_calls() if hasattr(ev, "get_function_calls") else []

    def _match_and_emit(fr) -> None:
        """Match a FunctionResponse to its pending FunctionCall and emit a step."""
        raw = fr.response or {}
        if isinstance(raw, dict):
            # ADK wraps string return values as {'result': value}
            obs_raw = raw.get("output", raw.get("result", raw))
        else:
            obs_raw = raw
        obs = _cap(str(obs_raw) if not isinstance(obs_raw, str) else obs_raw, OBS_CAP)

        # Match by id first, then FIFO fallback by function name.
        resp_id = str(fr.id) if fr.id else None
        p = None
        if resp_id and resp_id in pending:
            p = pending.pop(resp_id)
            if resp_id in pending_order:
                pending_order.remove(resp_id)
        elif pending_order:
            # Fallback: take the oldest pending call of the same name.
            match_id = next(
                (k for k in pending_order if pending[k]["act"].startswith(
                    "ask_human" if fr.name == "ask_human" else (fr.name or "")
                )),
                pending_order[0],  # or just the oldest
            )
            p = pending.pop(match_id)
            pending_order.remove(match_id)

        if p:
            steps.append({
                "thought": p["thought"],
                "act": p["act"],
                "obs": obs,
                "skill_used": bool(p.get("skill_used", False)),
            })
        else:
            steps.append({"thought": "", "act": "", "obs": obs, "skill_used": False})

    for event in adk_events:
        # Skip partial streaming events — only process final ones.
        if getattr(event, "partial", False):
            continue

        content = getattr(event, "content", None)
        if not content or not getattr(content, "parts", None):
            continue

        author = getattr(event, "author", "")

        # ── Priority 1: tool response events (observations) ───────────────────
        # NOTE: in google-adk 1.x these events carry author=AGENT_NAME, NOT
        # author="user" as older docs suggest.  Always check by content first.
        func_responses = _extract_func_responses(event)
        if func_responses:
            for fr in func_responses:
                _match_and_emit(fr)
            continue

        # ── Priority 2: model output with function calls ───────────────────────
        func_calls = _extract_func_calls(event)
        if func_calls:
            thought = ""
            for part in content.parts:
                t = getattr(part, "text", None)
                if t and not thought:
                    thought = _cap(t, THOUGHT_CAP)
                    break  # first text block = thought

            first = True
            for fc in func_calls:
                # Build act string matching run_codex.mjs / run_claude.mjs format.
                fc_name = fc.name or ""
                fc_args = fc.args or {}
                if fc_name == "ask_human":
                    q   = str(fc_args.get("question", ""))
                    act = _cap(f"ask_human {q}", ACT_CAP)
                elif fc_name == "bash":
                    cmd = str(fc_args.get("command", ""))
                    act = _cap(cmd, ACT_CAP)
                elif fc_name == "str_replace_editor":
                    command = str(fc_args.get("command", ""))
                    path    = str(fc_args.get("path", ""))
                    act = _cap(f"str_replace_editor {command} {path}".strip(), ACT_CAP)
                else:
                    act = _cap(f"{fc_name}: {json.dumps(fc_args)}", ACT_CAP)

                call_id = str(fc.id) if fc.id else f"_fc_{len(pending)}"
                pending[call_id] = {
                    "thought": thought if first else "",
                    "act":     act,
                    "skill_used": _is_explicit_skill_tool_call(fc_name),
                }
                pending_order.append(call_id)
                first = False
            continue

        # ── Priority 3: text-only model turn (standalone thought) ─────────────
        if author == AGENT_NAME:
            thought = ""
            for part in content.parts:
                t = getattr(part, "text", None)
                if t and not thought:
                    thought = _cap(t, THOUGHT_CAP)
                    break
            if thought:
                steps.append({"thought": thought, "act": "", "obs": ""})

    # Flush tool calls that never received a response (e.g. interrupted by timeout).
    for call_id in pending_order:
        p = pending.pop(call_id, None)
        if p:
            steps.append({
                "thought": p["thought"],
                "act":     p["act"],
                "obs":     "[no observation — tool call was interrupted]",
                "skill_used": bool(p.get("skill_used", False)),
            })

    return steps


# ── Stats computation ─────────────────────────────────────────────────────────

def compute_stats(
    all_events: list[dict],
    trajectory_steps: list[dict],
    num_blockers_total: int,
) -> dict:
    """
    Mirrors computeTrajectoryStats from run_claude.mjs and run_codex.mjs.

    all_events items are plain dicts (not ADK Event objects) — they are
    the human_input_* events returned by the sidecar and ask_question_full_info_mode
    events emitted directly by the ask_human tool function.
    """
    num_questions          = 0
    num_questions_approval = 0
    num_questions_full_info = 0
    num_blockers_resolved  = 0
    num_skill_calls        = 0

    for ev in all_events:
        ev_type = ev.get("type", "")
        if ev_type == "human_input_raw_event":
            rt = ev.get("request_type", "")
            if rt in ASK_HUMAN_REQUEST_TYPES:
                num_questions += 1
            elif rt in ("approval", "permission"):
                num_questions_approval += 1
        elif ev_type == "ask_question_full_info_mode":
            num_questions_full_info += 1
        elif ev_type == "human_input_result":
            bid = ev.get("result", {}).get("blocker_id")
            if (
                bid
                and bid != UNKNOWN_BLOCKER_ID
                and ev.get("result", {}).get("status") == "answered"
            ):
                num_blockers_resolved += 1

    # Count explicit skill tool invocations from trajectory steps.
    # Only ADK trajectories include this key, and only when confidently detected.
    for step in trajectory_steps:
        if bool(step.get("skill_used", False)):
            num_skill_calls += 1

    return {
        "num_steps":                len(trajectory_steps),
        "num_questions":            num_questions,
        "num_questions_approval":   num_questions_approval,
        "num_total_questions":      num_questions + num_questions_approval,
        "num_questions_full_info":  num_questions_full_info,
        "num_blockers_resolved":    num_blockers_resolved,
        "num_blockers_total":       num_blockers_total,
        "num_skill_calls":          num_skill_calls,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

    # ── 1. Read task data ─────────────────────────────────────────────────────
    metadata         = json.loads(Path(TASK_DIR, "metadata.json").read_text())
    problem_stmt     = Path(TASK_DIR, "problem_statement.txt").read_text()
    uid: str         = str(metadata.get("uid") or metadata.get("instance_id") or "unknown")

    # ── 2. Load blockers (full_info mode) + count total (both modes) ──────────
    blockers: list[dict] = []
    num_blockers_total   = 0
    registry_path = Path(TASK_DIR, "blocker_registry.json")
    if registry_path.exists():
        registry = json.loads(registry_path.read_text())
        entries  = registry.get("entries") or registry.get("blockers") or []
        if isinstance(registry, list):
            entries = registry
        num_blockers_total = len(entries)
        if MODE == "full_info":
            blockers = [
                {"description": e.get("description", ""), "resolution": e.get("resolution", "")}
                for e in entries
            ]

    # ── 3. Build prompt and instruction ───────────────────────────────────────
    prompt      = build_swe_prompt(problem_stmt, MODE, blockers)
    instruction = _build_instruction(MODE)

    # ── 4. Write attempt metadata ─────────────────────────────────────────────
    _write_json(Path(OUTPUT_DIR, "attempt.json"), {
        "run_id":     RUN_ID,
        "uid":        uid,
        "mode":       MODE,
        "pass_index": PASS_INDEX,
        "harness":    "adk",
        "model":      ADK_MODEL,
        "max_turns":  MAX_TURNS,
        "timeout_ms": TIMEOUT_MS,
        "workspace":  WORKSPACE,
        "task_dir":   TASK_DIR,
        "output_dir": OUTPUT_DIR,
        "started_at": _now_iso(),
        "prompt":     prompt,
    })

    # ── 5. Start ask_human sidecar (always — handles both modes) ──────────────
    # The sidecar handles full_info gracefully (returns UNKNOWN_RESOLUTION without
    # calling the LLM judge) so we keep the start/stop logic unconditional.
    global _sidecar_proc, _sidecar_url
    try:
        _sidecar_proc, _sidecar_url = _start_sidecar(uid)
    except Exception as exc:
        err = f"Failed to start ask_human sidecar: {exc}"
        print(f"[run_adk] ERROR: {err}", file=sys.stderr)
        _write_json(Path(OUTPUT_DIR, "result.json"), {
            "patch_bytes":  0,
            "num_steps":    0,
            "completed_at": _now_iso(),
            "sdk_error":    err,
            "timeout":      False,
            "stop_reason":  "sidecar_start_failed",
        })
        return

    # per-run event log: human_input_* events from sidecar + ask_question_full_info_mode
    all_events: list[dict] = []
    # ADK event stream (Event objects from runner.run_async)
    adk_events: list       = []

    # ── 6. Define agent tools (SWE-like surface) ─────────────────────────────
    file_history: dict[str, list[str]] = {}

    async def bash(command: str) -> str:
        """
        Run a bash command in the repository workspace (/app) and return the
        combined stdout + stderr output. Use this for exploration (ls, grep,
        cat), running tests (pytest, make test), and any other shell operations.
        """
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=WORKSPACE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
                return "[command timed out after 300 seconds]"
            output = (stdout + stderr).decode("utf-8", errors="replace")
            return output[:OBS_CAP] if len(output) > OBS_CAP else output
        except Exception as exc:
            return f"[bash error: {exc}]"

    async def ask_human(question: str) -> str:
        """
        Ask a human expert a focused question about the implementation requirements.
        Submit only ONE specific question at a time. The expert can only answer
        questions about what the code should do, not how to implement it.
        """
        loop = asyncio.get_event_loop()
        payload = {
            "question":          question,
            "options":           [],
            "context":           {},
            "request_type":      "clarification",
            "native_event_type": "adk.ask_human",
            "raw_event":         {"question": question},
        }
        try:
            result = await loop.run_in_executor(
                None, _sidecar_ask_sync, _sidecar_url, payload
            )
        except Exception:
            # Mirror the canonical SWE-agent ask_human tool behaviour:
            # any HTTP / connection failure returns exactly CANT_ANSWER so
            # the agent can retry on the next turn.
            return "can't answer (perhaps transient hiccup)"

        # Append human_input_* events (ask_human mode) or ask_question_full_info_mode
        # event (full_info mode) so compute_stats() can count them.
        for ev in result.get("events", []):
            all_events.append(ev)

        return str(result.get("resolution", UNKNOWN_RESOLUTION))

    async def str_replace_editor(
        command: str,
        path: str,
        file_text: Optional[str] = None,
        view_range: Optional[list[int]] = None,
        old_str: Optional[str] = None,
        new_str: Optional[str] = None,
        insert_line: Optional[int] = None,
    ) -> str:
        """
        SWE-like editor tool with Anthropic-style API:
        view/create/str_replace/insert/undo_edit.
        """
        command = str(command or "").strip()
        if command not in {"view", "create", "str_replace", "insert", "undo_edit"}:
            return (
                "Unrecognized command. Allowed commands are: "
                "view, create, str_replace, insert, undo_edit."
            )
        resolved, err = _normalize_workspace_path(path)
        if err:
            return err
        assert resolved is not None
        key = str(resolved)

        if command == "view":
            if resolved.is_dir():
                if view_range:
                    return "The `view_range` parameter is not allowed when `path` points to a directory."
                lines: list[str] = []
                root_depth = len(resolved.parts)
                for root, dirs, files in os.walk(resolved):
                    rel_depth = len(Path(root).parts) - root_depth
                    if rel_depth > 2:
                        dirs[:] = []
                        continue
                    dirs[:] = [d for d in dirs if not d.startswith(".")]
                    files = [f for f in files if not f.startswith(".")]
                    rel_root = Path(root).relative_to(resolved)
                    display_root = "." if str(rel_root) == "." else str(rel_root)
                    lines.append(f"{display_root}/")
                    lines.extend(f"  {f}" for f in sorted(files))
                return _truncate_response(
                    "Here's the files and directories up to 2 levels deep "
                    f"in {resolved}, excluding hidden items:\n" + "\n".join(lines) + "\n"
                )

            if not resolved.exists():
                return f"The path {resolved} does not exist. Please provide a valid path."
            content = resolved.read_text(encoding="utf-8", errors="replace")
            init_line = 1
            if view_range:
                if len(view_range) != 2 or not all(isinstance(i, int) for i in view_range):
                    return "Invalid `view_range`. It should be a list of two integers."
                lines = content.split("\n")
                total = len(lines)
                start, end = view_range
                if start < 1 or start > total:
                    return (
                        f"Invalid `view_range`: {view_range}. "
                        f"First element should be in [1, {total}]."
                    )
                if end == -1:
                    end = total
                if end < start or end > total:
                    return (
                        f"Invalid `view_range`: {view_range}. "
                        f"Second element should be -1 or in [{start}, {total}]."
                    )
                init_line = start
                content = "\n".join(lines[start - 1 : end])
            return _truncate_response(_format_cat_n(content, str(resolved), init_line=init_line))

        if command == "create":
            if resolved.exists():
                return f"File already exists at: {resolved}. Cannot overwrite via `create`."
            if file_text is None:
                return "Parameter `file_text` is required for command: create."
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(file_text, encoding="utf-8")
            file_history.setdefault(key, []).append("")
            return f"File created successfully at: {resolved}"

        if not resolved.exists():
            return f"The path {resolved} does not exist. Please provide a valid path."
        if resolved.is_dir():
            return f"The path {resolved} is a directory and only `view` can be used on directories."

        original = resolved.read_text(encoding="utf-8", errors="replace").expandtabs()

        if command == "str_replace":
            if old_str is None:
                return "Parameter `old_str` is required for command: str_replace."
            replacement = (new_str or "").expandtabs()
            needle = old_str.expandtabs()
            occurrences = original.count(needle)
            if occurrences == 0:
                return (
                    f"No replacement was performed, old_str `{needle}` did not appear verbatim in {resolved}."
                )
            if occurrences > 1:
                return (
                    "No replacement was performed. Multiple occurrences of old_str "
                    f"`{needle}` found. Please ensure it is unique."
                )
            updated = original.replace(needle, replacement)
            resolved.write_text(updated, encoding="utf-8")
            file_history.setdefault(key, []).append(original)
            return _truncate_response(
                f"The file {resolved} has been edited. "
                "Review the changes and make sure they are as expected."
            )

        if command == "insert":
            if insert_line is None:
                return "Parameter `insert_line` is required for command: insert."
            if new_str is None:
                return "Parameter `new_str` is required for command: insert."
            lines = original.split("\n")
            if insert_line < 0 or insert_line > len(lines):
                return (
                    f"Invalid `insert_line` parameter: {insert_line}. "
                    f"It should be within [0, {len(lines)}]."
                )
            insert_lines = new_str.expandtabs().split("\n")
            updated_lines = lines[:insert_line] + insert_lines + lines[insert_line:]
            resolved.write_text("\n".join(updated_lines), encoding="utf-8")
            file_history.setdefault(key, []).append(original)
            return _truncate_response(
                f"The file {resolved} has been edited via insert at line {insert_line}."
            )

        # command == "undo_edit"
        history = file_history.get(key) or []
        if not history:
            return f"No edit history found for {resolved}."
        prev = history.pop()
        resolved.write_text(prev, encoding="utf-8")
        return f"Last edit to {resolved} undone successfully."

    adk_skill_toolsets: list[Any] = []
    try:
        from google.adk.skills import load_skill_from_dir
        from google.adk.tools import skill_toolset

        skill_dir = _install_workspace_skill_for_discovery(WORKSPACE, SKILL_TOOL_NAME)
        loaded_skill = load_skill_from_dir(skill_dir)
        adk_skill_toolsets = [skill_toolset.SkillToolset(skills=[loaded_skill])]
    except Exception as exc:
        print(f"[run_adk] WARN: failed to initialize ADK skills: {exc}", file=sys.stderr)

    # ── 7. Create ADK agent ───────────────────────────────────────────────────
    # Route LiteLlm calls through the LiteLLM proxy by passing api_base and
    # api_key.  The "gemini/" model prefix is kept as-is so litellm uses the
    # Google AI Studio handler, which respects api_base without needing local
    # GCP credentials (unlike the "vertex_ai/" handler which tries ADC first).
    _litellm_kwargs: dict = {}
    if LITELLM_BASE_URL:
        _litellm_kwargs["api_base"] = LITELLM_BASE_URL
    if LITELLM_API_KEY:
        _litellm_kwargs["api_key"] = LITELLM_API_KEY
    # Per LiteLLM call timeout and retries (3 tries total, 20 minutes each by default).
    _litellm_kwargs["timeout"] = LITELLM_CALL_TIMEOUT_S
    _litellm_kwargs["num_retries"] = max(0, STEP_LITELLM_TRIES - 1)
    if ADK_REASONING_EFFORT:
        # Best-effort hint forwarded to LiteLLM; unsupported providers may ignore it.
        _litellm_kwargs["reasoning_effort"] = ADK_REASONING_EFFORT

    # The user-turn message is constant across retry attempts.
    new_message = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=prompt)],
    )

    # ── 8. Run agent with up to MAX_RETRIES attempts ───────────────────────────
    # Each attempt re-creates the agent and session from scratch so there is no
    # stale state carried over from a failed attempt.  The sidecar (started in
    # step 5) is shared across all attempts; its event lists are cleared on retry.
    # Retries occur only on sdk_error (transient LLM / network failures); timeout
    # and clean exits (complete / max_turns) exit the loop immediately.
    MAX_RETRIES   = STEP_LITELLM_TRIES
    timed_out     = False
    sdk_error_msg: Optional[str] = None
    stop_reason   = "complete"

    _run_wall_start = time.monotonic()

    for _attempt in range(1, MAX_RETRIES + 1):
        # Reset per-attempt mutable state (lists cleared in-place so closures remain valid)
        all_events.clear()
        adk_events.clear()
        file_history.clear()
        timed_out     = False
        sdk_error_msg = None
        stop_reason   = "complete"

        # Re-create agent + session (fresh state for each attempt)
        agent = LlmAgent(
            name=AGENT_NAME,
            model=LiteLlm(model=ADK_MODEL, **_litellm_kwargs),
            tools=[bash, str_replace_editor, ask_human, *adk_skill_toolsets],
            instruction=instruction,
            # temperature=1.0 matches ask_config_gemini_3-1_pro_preview_customtools.yaml
            generate_content_config=genai_types.GenerateContentConfig(temperature=1.0),
        )
        session_service = InMemorySessionService()
        runner          = Runner(
            agent=agent,
            app_name="trust_horizon_swe",
            session_service=session_service,
        )
        session = await session_service.create_session(
            app_name="trust_horizon_swe",
            user_id="swe_user",
        )
        run_config = RunConfig(max_llm_calls=MAX_TURNS)

        # _run_agent closes over `runner`, `session`, `adk_events` (the module-level
        # list) by name — they are re-looked-up on each call, so reassigning those
        # variables above before defining _run_agent is safe.
        async def _run_agent() -> None:
            nonlocal timed_out, sdk_error_msg, stop_reason
            try:
                async for event in runner.run_async(
                    user_id=session.user_id if hasattr(session, "user_id") else "swe_user",
                    session_id=session.id,
                    new_message=new_message,
                    run_config=run_config,
                ):
                    adk_events.append(event)
            except LlmCallsLimitExceededError:
                # Expected: agent used all its turns.  Partial results are still valid.
                stop_reason = "max_turns"
            except asyncio.CancelledError:
                # Raised when we cancel the task on timeout — handled below.
                timed_out   = True
                stop_reason = "timeout"
                raise
            except Exception as exc:
                sdk_error_msg = str(exc)
                stop_reason   = "sdk_error"

        # Each attempt is individually capped at the per-call timeout budget.
        # min() with remaining wall-clock ensures we never overshoot ATTEMPT_TIMEOUT_MS.
        PER_ATTEMPT_TIMEOUT_S = LITELLM_CALL_TIMEOUT_S
        elapsed_ms      = (time.monotonic() - _run_wall_start) * 1000
        remaining_ms    = max(0.0, TIMEOUT_MS - elapsed_ms)
        attempt_timeout = min(remaining_ms / 1000, PER_ATTEMPT_TIMEOUT_S)

        try:
            await asyncio.wait_for(_run_agent(), timeout=attempt_timeout)
        except asyncio.TimeoutError:
            timed_out   = True
            stop_reason = "timeout"
        except asyncio.CancelledError:
            timed_out   = True
            stop_reason = "timeout"

        # Retry transient failures and timeout-aborted runs up to STEP_LITELLM_TRIES.
        if stop_reason in {"sdk_error", "timeout"} and _attempt < MAX_RETRIES and remaining_ms > 0:
            label = f"[{uid[:12]}|{MODE}|p{PASS_INDEX}]"
            print(
                f"{label} {stop_reason} on attempt {_attempt}/{MAX_RETRIES}, retrying: {sdk_error_msg}",
                file=sys.stderr,
            )
            continue
        break

    # ── 9. Collect final patch and build outputs ──────────────────────────────
    patch_content = await _git_diff(WORKSPACE)

    trajectory_steps = extract_trajectory_steps(adk_events)
    stats            = compute_stats(all_events, trajectory_steps, num_blockers_total)

    _write_json(Path(OUTPUT_DIR, "trajectory.json"), trajectory_steps)
    _write_json(Path(OUTPUT_DIR, "stats.json"),      stats)

    patch_path = Path(OUTPUT_DIR, "patch.diff")
    patch_path.write_text(patch_content, encoding="utf-8")

    _write_json(Path(OUTPUT_DIR, "result.json"), {
        "patch_bytes":  len(patch_content.encode("utf-8")),
        "num_steps":    stats["num_steps"],
        "completed_at": _now_iso(),
        "sdk_error":    sdk_error_msg,
        "timeout":      timed_out,
        "stop_reason":  stop_reason,
    })

    # ── 10. Cleanup ───────────────────────────────────────────────────────────
    _stop_sidecar(_sidecar_proc)

    label = f"[{uid[:12]}|{MODE}|p{PASS_INDEX}]"
    if sdk_error_msg:
        print(f"{label} ADK error: {sdk_error_msg}", file=sys.stderr)
    elif timed_out:
        print(f"{label} timed out (max {TIMEOUT_MS // 1000}s)", file=sys.stderr)
    else:
        print(f"{label} done  steps={stats['num_steps']}  "
              f"questions={stats['num_questions']}  "
              f"questions_full_info={stats['num_questions_full_info']}  "
              f"patch_bytes={len(patch_content.encode('utf-8'))}")


if __name__ == "__main__":
    asyncio.run(main())
