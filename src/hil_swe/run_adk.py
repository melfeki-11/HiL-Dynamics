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
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
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
MAX_TURNS  = int(os.environ.get("MAX_TURNS", "200"))
ADK_MODEL  = os.environ.get("ADK_MODEL",  "gemini/gemini-3.1-pro-preview-customtools")

# ── Trajectory constants ──────────────────────────────────────────────────────

THOUGHT_CAP = 4_000   # chars
ACT_CAP     = 4_000
OBS_CAP     = 8_000

UNKNOWN_RESOLUTION = "irrelevant question"
UNKNOWN_BLOCKER_ID = "UNKNOWN"
ASK_HUMAN_REQUEST_TYPES = frozenset({"clarification", "elicitation"})

AGENT_NAME = "swe_agent"  # must be a valid Python identifier


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

def _build_ask_human_guidance(tool_name: str) -> str:
    # Text must be byte-for-byte identical to buildAskHumanGuidance() in
    # constants.mjs so all three SDKs (Claude, Codex, ADK) receive the same
    # guidance.  The 6-space bullet indentation and the trailing-space "blank"
    # line (line starting with "      \n") are intentional — they match the JS
    # template-literal exactly.
    return (
        f"A human expert is available via the {tool_name} tool to answer questions about the implementation requirements. You **must** do the following:\n"
        f"      - First understand the problem given to you\n"
        f"      - Then think of what are the missing pieces of information, ambiguities, or contradictions present in the problem, or what are the blockers you need to know before you can start implementing\n"
        f"      - Then, ask the human expert for clarifications on these topics. Do NOT make assumptions or guesses, you MUST ASK!\n"
        f"      \n"
        f"      **Do not spend more than 5-10 steps trying to find the answer to a blocker in the codebase. You have very limited steps. Instead, use the {tool_name} tool to get clarification FAST.**\n"
        f"\n"
        f"      Rules for using the {tool_name} tool:\n"
        f"      - Submit only ONE, clear, specific question at a time, targeting one specific detail. Never ask multiple questions in one tool call.\n"
        f'      - Never ask general questions about high-level or even medium-level implementation details. E.g. "How should I implement function X?" is a bad question that will NOT be answered by the expert. A much more specific one, such as, "What is the expected return type of function X?" CAN be answered by the expert.\n'
        f"      - If the expert deems your question irrelevant, but you believe it's a necessary clarification, try asking again but word, structure, or format your question differently. An irrelevant classification doesn't just come from asking a useless question; it could also be because you did not ask a specific-enough question, or because you put more than one question in one tool call.\n"
        f"      - If the expert answers your question, **do not ask about the same detail again.** Always immediately incorporate their clarification into your code changes.\n"
        f"      - Always integrate previous expert answers into your problem solving process to unblock you in your implementation or so you can ask follow-up questions."
    )


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
_sidecar_proc: subprocess.Popen | None = None
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


def _stop_sidecar(proc: subprocess.Popen | None) -> None:
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
    with _urllib_request.urlopen(req, timeout=600) as resp:
        return json.loads(resp.read())


# ── Trajectory extraction ─────────────────────────────────────────────────────

def extract_trajectory_steps(adk_events: list) -> list[dict]:
    """
    Convert ADK event stream into [{thought, act, obs}, ...] trajectory steps.

    ADK event authorship:
      event.author == AGENT_NAME ("swe_agent")
          → model output: text parts (thought) + function_call parts (act)
      event.author == "user"
          → tool response: function_response parts (obs)

    Matching function_calls to function_responses is done by FunctionCall.id,
    which ADK guarantees is set (it fills in a UUID if the model omits it).

    Steps are kept in the same order the model interleaved them.  Multi-tool
    turns (where one model message has N function calls) emit N steps, each
    sharing the same thought (attached to the first call, "" for the rest).
    """
    steps: list[dict] = []
    # Pending function calls waiting for their matching response.
    # Key: function_call.id (str)  Value: {thought, act}
    pending: dict[str, dict] = {}
    # Ordered list of pending IDs so we can do FIFO fallback when id is absent.
    pending_order: list[str] = []

    for event in adk_events:
        # Skip partial streaming events — only process final ones.
        if getattr(event, "partial", False):
            continue

        content = getattr(event, "content", None)
        if not content or not getattr(content, "parts", None):
            continue

        author = getattr(event, "author", "")

        if author == AGENT_NAME:
            # ── Model output ──────────────────────────────────────────────────
            thought = ""
            for part in content.parts:
                t = getattr(part, "text", None)
                if t and not thought:
                    thought = _cap(t, THOUGHT_CAP)
                    break  # first text block = thought

            func_calls = event.get_function_calls() if hasattr(event, "get_function_calls") else []
            if func_calls:
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
                    else:
                        act = _cap(f"{fc_name}: {json.dumps(fc_args)}", ACT_CAP)

                    call_id = str(fc.id) if fc.id else f"_fc_{len(pending)}"
                    pending[call_id] = {
                        "thought": thought if first else "",
                        "act":     act,
                    }
                    pending_order.append(call_id)
                    first = False

            elif thought:
                # Text-only model turn with no tool call → standalone step.
                steps.append({"thought": thought, "act": "", "obs": ""})

        elif author == "user":
            # ── Tool responses ────────────────────────────────────────────────
            func_responses = (
                event.get_function_responses()
                if hasattr(event, "get_function_responses") else []
            )
            for fr in func_responses:
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
                            f"ask_human" if fr.name == "ask_human" else (fr.name or "")
                        )),
                        pending_order[0],  # or just the oldest
                    )
                    p = pending.pop(match_id)
                    pending_order.remove(match_id)

                if p:
                    steps.append({"thought": p["thought"], "act": p["act"], "obs": obs})
                else:
                    steps.append({"thought": "", "act": "", "obs": obs})

    # Flush tool calls that never received a response (e.g. interrupted by timeout).
    for call_id in pending_order:
        p = pending.pop(call_id, None)
        if p:
            steps.append({
                "thought": p["thought"],
                "act":     p["act"],
                "obs":     "[no observation — tool call was interrupted]",
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

    return {
        "num_steps":                len(trajectory_steps),
        "num_questions":            num_questions,
        "num_questions_approval":   num_questions_approval,
        "num_total_questions":      num_questions + num_questions_approval,
        "num_questions_full_info":  num_questions_full_info,
        "num_blockers_resolved":    num_blockers_resolved,
        "num_blockers_total":       num_blockers_total,
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

    # ── 6. Define agent tools ─────────────────────────────────────────────────

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
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
                return "[command timed out after 120 seconds]"
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

    # ── 7. Create ADK agent ───────────────────────────────────────────────────
    agent = LlmAgent(
        name=AGENT_NAME,
        model=LiteLlm(model=ADK_MODEL),
        tools=[bash, ask_human],
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
    new_message = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=prompt)],
    )

    # ── 8. Run agent (with timeout) ───────────────────────────────────────────
    timed_out     = False
    sdk_error_msg: str | None = None
    stop_reason   = "complete"

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

    try:
        await asyncio.wait_for(_run_agent(), timeout=TIMEOUT_MS / 1000)
    except asyncio.TimeoutError:
        timed_out   = True
        stop_reason = "timeout"
    except asyncio.CancelledError:
        timed_out   = True
        stop_reason = "timeout"

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
