"""
Unit tests for run_adk.py pure helper functions.

Tests cover:
  1. extract_trajectory_steps  — ADK Event list → [{thought, act, obs}]
  2. compute_stats             — all_events list → stats dict
  3. build_swe_prompt          — prompt construction for ask_human + full_info
  4. _build_instruction        — system prompt with/without ask_human guidance

Run with:
  python3 -m pytest tests/run_adk_test.py -v
  (from trust_horizon root)
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

# Allow imports from scripts/ without installation
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src" / "hil_swe") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src" / "hil_swe"))

# Suppress ADK warning before import
os.environ.setdefault("ADK_SUPPRESS_GEMINI_LITELLM_WARNINGS", "true")

from run_adk import (
    AGENT_NAME,
    OBS_CAP,
    THOUGHT_CAP,
    UNKNOWN_BLOCKER_ID,
    UNKNOWN_RESOLUTION,
    _build_instruction,
    build_swe_prompt,
    compute_stats,
    extract_trajectory_steps,
)

from google.adk.events import Event
from google.genai import types as genai_types


# ── ADK Event factory helpers ─────────────────────────────────────────────────

def _make_model_text_event(text: str, partial: bool = False) -> Event:
    """Create a model event with a single text part (thought / final response)."""
    return Event(
        author=AGENT_NAME,
        partial=partial,
        content=genai_types.Content(
            role="model",
            parts=[genai_types.Part(text=text)],
        ),
        id=Event.new_id(),
        invocation_id="test-inv",
    )


def _make_model_tool_call_event(
    tool_name: str,
    args: dict,
    call_id: str = "call-1",
    thought: str = "",
) -> Event:
    """Create a model event with an optional text thought and a function_call."""
    parts = []
    if thought:
        parts.append(genai_types.Part(text=thought))
    parts.append(genai_types.Part(
        function_call=genai_types.FunctionCall(name=tool_name, args=args, id=call_id),
    ))
    return Event(
        author=AGENT_NAME,
        partial=False,
        content=genai_types.Content(role="model", parts=parts),
        id=Event.new_id(),
        invocation_id="test-inv",
    )


def _make_tool_response_event(
    tool_name: str,
    response: dict,
    call_id: str = "call-1",
) -> Event:
    """Create a user event with a function_response part."""
    return Event(
        author="user",
        partial=False,
        content=genai_types.Content(
            role="user",
            parts=[genai_types.Part(
                function_response=genai_types.FunctionResponse(
                    name=tool_name,
                    response=response,
                    id=call_id,
                ),
            )],
        ),
        id=Event.new_id(),
        invocation_id="test-inv",
    )


# ── 1. extract_trajectory_steps ───────────────────────────────────────────────

class TestExtractTrajectorySteps(unittest.TestCase):

    def test_empty_events(self):
        self.assertEqual(extract_trajectory_steps([]), [])

    def test_partial_events_are_skipped(self):
        events = [_make_model_text_event("I am thinking...", partial=True)]
        self.assertEqual(extract_trajectory_steps(events), [])

    def test_text_only_model_event_becomes_thought_step(self):
        events = [_make_model_text_event("I should look at the files first.")]
        steps = extract_trajectory_steps(events)
        self.assertEqual(len(steps), 1)
        self.assertIn("I should look at the files first.", steps[0]["thought"])
        self.assertEqual(steps[0]["act"], "")
        self.assertEqual(steps[0]["obs"], "")

    def test_bash_tool_call_produces_step(self):
        events = [
            _make_model_tool_call_event(
                "bash", {"command": "ls /app"}, call_id="c1", thought="Let me explore"
            ),
            _make_tool_response_event("bash", {"result": "src tests"}, call_id="c1"),
        ]
        steps = extract_trajectory_steps(events)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["thought"], "Let me explore")
        self.assertEqual(steps[0]["act"],    "ls /app")
        self.assertEqual(steps[0]["obs"],    "src tests")

    def test_bash_obs_has_result_key(self):
        # ADK wraps string return values as {'result': <value>}
        events = [
            _make_model_tool_call_event("bash", {"command": "echo hi"}, call_id="c2"),
            _make_tool_response_event("bash", {"result": "hi\n"}, call_id="c2"),
        ]
        steps = extract_trajectory_steps(events)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["obs"], "hi\n")

    def test_bash_obs_with_output_key(self):
        # Some tools may return {'output': ...} instead of {'result': ...}
        events = [
            _make_model_tool_call_event("bash", {"command": "pwd"}, call_id="c3"),
            _make_tool_response_event("bash", {"output": "/app\n"}, call_id="c3"),
        ]
        steps = extract_trajectory_steps(events)
        self.assertEqual(steps[0]["obs"], "/app\n")

    def test_ask_human_tool_call_produces_ask_human_act(self):
        events = [
            _make_model_tool_call_event(
                "ask_human",
                {"question": "What is the expected output format?"},
                call_id="c4",
            ),
            _make_tool_response_event(
                "ask_human", {"result": "JSON with a 'data' key"}, call_id="c4"
            ),
        ]
        steps = extract_trajectory_steps(events)
        self.assertEqual(len(steps), 1)
        self.assertTrue(steps[0]["act"].startswith("ask_human "))
        self.assertIn("What is the expected output format?", steps[0]["act"])
        self.assertEqual(steps[0]["obs"], "JSON with a 'data' key")

    def test_multi_step_sequence(self):
        events = [
            _make_model_text_event("Look at the tests first."),
            _make_model_tool_call_event("bash", {"command": "pytest"}, call_id="c5"),
            _make_tool_response_event("bash", {"result": "5 failed"}, call_id="c5"),
            _make_model_tool_call_event(
                "bash", {"command": "cat src/foo.py"}, call_id="c6", thought="Edit the file"
            ),
            _make_tool_response_event("bash", {"result": "def foo(): pass"}, call_id="c6"),
        ]
        steps = extract_trajectory_steps(events)
        self.assertEqual(len(steps), 3)
        # Step 0: thought-only
        self.assertEqual(steps[0]["thought"], "Look at the tests first.")
        self.assertEqual(steps[0]["act"],     "")
        # Step 1: bash with no thought (model event had no text before fc)
        self.assertEqual(steps[1]["act"],     "pytest")
        self.assertEqual(steps[1]["obs"],     "5 failed")
        # Step 2: bash with thought
        self.assertEqual(steps[2]["thought"], "Edit the file")
        self.assertEqual(steps[2]["act"],     "cat src/foo.py")

    def test_interrupted_tool_call_flushed(self):
        # Tool call that never received a response (e.g. timeout)
        events = [
            _make_model_tool_call_event("bash", {"command": "sleep 999"}, call_id="c7"),
            # no matching tool response event
        ]
        steps = extract_trajectory_steps(events)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["act"], "sleep 999")
        self.assertIn("interrupted", steps[0]["obs"])

    def test_long_thought_is_capped(self):
        long_text = "x" * (THOUGHT_CAP + 100)
        events = [_make_model_text_event(long_text)]
        steps = extract_trajectory_steps(events)
        self.assertEqual(len(steps), 1)
        self.assertLessEqual(len(steps[0]["thought"]), THOUGHT_CAP + 20)  # +20 for truncation suffix
        self.assertTrue(steps[0]["thought"].endswith("[truncated]"))

    def test_long_obs_is_capped(self):
        long_output = "y" * (OBS_CAP + 100)
        events = [
            _make_model_tool_call_event("bash", {"command": "cat big"}, call_id="c8"),
            _make_tool_response_event("bash", {"result": long_output}, call_id="c8"),
        ]
        steps = extract_trajectory_steps(events)
        self.assertLessEqual(len(steps[0]["obs"]), OBS_CAP + 20)
        self.assertTrue(steps[0]["obs"].endswith("[truncated]"))

    def test_all_steps_have_thought_act_obs_keys(self):
        events = [
            _make_model_text_event("thinking"),
            _make_model_tool_call_event("bash", {"command": "ls"}, call_id="c9"),
            _make_tool_response_event("bash", {"result": "ok"}, call_id="c9"),
        ]
        steps = extract_trajectory_steps(events)
        for step in steps:
            self.assertIn("thought", step)
            self.assertIn("act",     step)
            self.assertIn("obs",     step)


# ── 2. compute_stats ──────────────────────────────────────────────────────────

class TestComputeStats(unittest.TestCase):

    def test_empty_events_zero_stats(self):
        stats = compute_stats([], [], 0)
        self.assertEqual(stats["num_steps"],                0)
        self.assertEqual(stats["num_questions"],            0)
        self.assertEqual(stats["num_questions_approval"],   0)
        self.assertEqual(stats["num_total_questions"],      0)
        self.assertEqual(stats["num_questions_full_info"],  0)
        self.assertEqual(stats["num_blockers_resolved"],    0)
        self.assertEqual(stats["num_blockers_total"],       0)

    def test_clarification_events_counted_as_num_questions(self):
        events = [
            {"type": "human_input_raw_event", "request_type": "clarification"},
            {"type": "human_input_raw_event", "request_type": "elicitation"},
        ]
        stats = compute_stats(events, [], 0)
        self.assertEqual(stats["num_questions"],           2)
        self.assertEqual(stats["num_questions_full_info"], 0)

    def test_approval_events_counted_separately(self):
        events = [
            {"type": "human_input_raw_event", "request_type": "approval"},
            {"type": "human_input_raw_event", "request_type": "permission"},
        ]
        stats = compute_stats(events, [], 0)
        self.assertEqual(stats["num_questions"],          0)
        self.assertEqual(stats["num_questions_approval"], 2)
        self.assertEqual(stats["num_total_questions"],    2)

    def test_full_info_events_counted_in_num_questions_full_info(self):
        events = [
            {"type": "ask_question_full_info_mode", "question": "Q1"},
            {"type": "ask_question_full_info_mode", "question": "Q2"},
        ]
        stats = compute_stats(events, [], 0)
        self.assertEqual(stats["num_questions_full_info"], 2)
        self.assertEqual(stats["num_questions"],           0)  # NOT in ask_human counter
        self.assertEqual(stats["num_total_questions"],     0)  # NOT in total either

    def test_full_info_and_ask_human_counted_independently(self):
        events = [
            {"type": "human_input_raw_event", "request_type": "clarification"},
            {"type": "ask_question_full_info_mode", "question": "Q-fi"},
            {"type": "human_input_raw_event", "request_type": "elicitation"},
        ]
        stats = compute_stats(events, [], 0)
        self.assertEqual(stats["num_questions"],           2)
        self.assertEqual(stats["num_questions_full_info"], 1)
        self.assertEqual(stats["num_total_questions"],     2)

    def test_answered_blockers_counted(self):
        events = [
            {"type": "human_input_result", "result": {"blocker_id": "b1", "status": "answered"}},
            {"type": "human_input_result", "result": {"blocker_id": "b2", "status": "answered"}},
            {"type": "human_input_result", "result": {"blocker_id": UNKNOWN_BLOCKER_ID, "status": "answered"}},  # excluded
            {"type": "human_input_result", "result": {"blocker_id": "b3", "status": "rejected"}},  # excluded
        ]
        stats = compute_stats(events, [], 5)
        self.assertEqual(stats["num_blockers_resolved"], 2)
        self.assertEqual(stats["num_blockers_total"],    5)

    def test_num_steps_from_trajectory_length(self):
        steps = [
            {"thought": "", "act": "ls", "obs": ""},
            {"thought": "", "act": "pytest", "obs": "ok"},
        ]
        stats = compute_stats([], steps, 0)
        self.assertEqual(stats["num_steps"], 2)


# ── 3. build_swe_prompt ───────────────────────────────────────────────────────

class TestBuildSwePrompt(unittest.TestCase):

    PROBLEM = "Fix the bug in module X."

    def test_ask_human_prompt_has_pr_description(self):
        prompt = build_swe_prompt(self.PROBLEM, "ask_human", [])
        self.assertIn("<pr_description>", prompt)
        self.assertIn(self.PROBLEM, prompt)

    def test_ask_human_prompt_has_no_additional_context(self):
        prompt = build_swe_prompt(self.PROBLEM, "ask_human", [])
        self.assertNotIn("## Additional Context", prompt)

    def test_full_info_no_blockers_matches_ask_human(self):
        p1 = build_swe_prompt(self.PROBLEM, "ask_human",  [])
        p2 = build_swe_prompt(self.PROBLEM, "full_info",  [])
        self.assertEqual(p1, p2)

    def test_full_info_with_blockers_has_additional_context(self):
        blockers = [
            {"description": "Return type", "resolution": "Returns int."},
            {"description": "Edge case",   "resolution": "Raise ValueError."},
        ]
        prompt = build_swe_prompt(self.PROBLEM, "full_info", blockers)
        self.assertIn("## Additional Context", prompt)
        self.assertIn("Return type", prompt)
        self.assertIn("Returns int.", prompt)
        self.assertIn("Edge case",   prompt)

    def test_full_info_blockers_inside_pr_description(self):
        # Additional Context must be INSIDE <pr_description> (not after </pr_description>)
        blockers = [{"description": "D", "resolution": "R"}]
        prompt = build_swe_prompt(self.PROBLEM, "full_info", blockers)
        pr_close = prompt.index("</pr_description>")
        ctx_pos  = prompt.index("## Additional Context")
        self.assertLess(ctx_pos, pr_close,
                        "Additional Context must appear inside <pr_description>")

    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            build_swe_prompt(self.PROBLEM, "unknown_mode", [])


# ── 4. _build_instruction ─────────────────────────────────────────────────────

class TestBuildInstruction(unittest.TestCase):

    def test_ask_human_mode_contains_base_and_guidance(self):
        instr = _build_instruction("ask_human")
        self.assertIn("helpful assistant", instr)
        self.assertIn("ask_human", instr)         # tool name in guidance
        self.assertIn("human expert", instr)

    def test_full_info_mode_contains_only_base(self):
        instr = _build_instruction("full_info")
        self.assertIn("helpful assistant", instr)
        self.assertNotIn("human expert", instr)   # no ask_human guidance
        self.assertNotIn("irrelevant question", instr)

    def test_ask_human_guidance_references_tool_name(self):
        instr = _build_instruction("ask_human")
        # The guidance must reference the exact tool name the model will see
        self.assertIn("ask_human tool", instr)


# ── 5. Sidecar integration tests ─────────────────────────────────────────────

import urllib.request as _urllib
import threading as _threading


def _make_sidecar_request(url: str, question: str) -> dict:
    """POST /ask to the running sidecar and return parsed JSON."""
    import json as _json
    body = _json.dumps({"question": question}).encode()
    req  = _urllib.Request(
        f"{url}/ask",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _urllib.urlopen(req, timeout=10) as resp:
        return _json.loads(resp.read())


class TestSidecarIntegration(unittest.TestCase):
    """Integration tests that actually spawn ask_human_sidecar.mjs."""

    def _start(self, mode: str, uid: str = "test-uid"):
        """Helper: start sidecar with a specific mode and return (proc, url).

        Temporarily patches os.environ so _start_sidecar inherits the right MODE
        and TASK_DIR.  Restores original values in a finally block.
        """
        from run_adk import _start_sidecar
        originals = {k: os.environ.get(k) for k in ("MODE", "TASK_DIR")}
        os.environ["MODE"]     = mode
        os.environ["TASK_DIR"] = str(_ROOT)
        try:
            proc, url = _start_sidecar(uid)
        finally:
            for k, v in originals.items():
                if v is None: os.environ.pop(k, None)
                else:         os.environ[k] = v
        return proc, url

    def test_health_endpoint_returns_ok(self):
        import json
        from run_adk import _stop_sidecar
        proc, url = self._start("full_info")
        try:
            resp = json.loads(_urllib.urlopen(f"{url}/health", timeout=5).read())
            self.assertTrue(resp["ok"])
            self.assertEqual(resp["mode"], "full_info")
        finally:
            _stop_sidecar(proc)

    def test_full_info_mode_returns_irrelevant_question(self):
        from run_adk import _stop_sidecar
        proc, url = self._start("full_info")
        try:
            result = _make_sidecar_request(url, "What should I return?")
            self.assertEqual(result["resolution"],      UNKNOWN_RESOLUTION)
            self.assertEqual(result["blocker_id"],      UNKNOWN_BLOCKER_ID)
            self.assertEqual(result["status"],          "unknown")
        finally:
            _stop_sidecar(proc)

    def test_full_info_mode_emits_ask_question_full_info_mode_event(self):
        from run_adk import _stop_sidecar
        proc, url = self._start("full_info")
        try:
            result = _make_sidecar_request(url, "Which class to edit?")
            self.assertEqual(len(result["events"]), 1)
            ev = result["events"][0]
            self.assertEqual(ev["type"],     "ask_question_full_info_mode")
            self.assertEqual(ev["question"], "Which class to edit?")
            self.assertIn("timestamp", ev)
        finally:
            _stop_sidecar(proc)

    def test_full_info_multiple_questions_each_emit_event(self):
        from run_adk import _stop_sidecar
        proc, url = self._start("full_info")
        try:
            r1 = _make_sidecar_request(url, "Question one?")
            r2 = _make_sidecar_request(url, "Question two?")
            self.assertEqual(r1["events"][0]["question"], "Question one?")
            self.assertEqual(r2["events"][0]["question"], "Question two?")
            # Each response is independent
            self.assertEqual(len(r1["events"]), 1)
            self.assertEqual(len(r2["events"]), 1)
        finally:
            _stop_sidecar(proc)

    def test_sidecar_ask_sync_helper_works(self):
        from run_adk import _sidecar_ask_sync, _stop_sidecar
        # Temporarily set MODE so _start works
        import json
        proc, url = self._start("full_info")
        try:
            result = _sidecar_ask_sync(url, {"question": "Sync helper test?"})
            self.assertEqual(result["resolution"], UNKNOWN_RESOLUTION)
            self.assertEqual(result["events"][0]["type"], "ask_question_full_info_mode")
        finally:
            _stop_sidecar(proc)

    def test_unknown_route_returns_404(self):
        from run_adk import _stop_sidecar
        import json
        proc, url = self._start("full_info")
        try:
            with self.assertRaises(Exception) as ctx:
                _urllib.urlopen(f"{url}/nonexistent", timeout=5)
            self.assertIn("404", str(ctx.exception))
        finally:
            _stop_sidecar(proc)


if __name__ == "__main__":
    unittest.main()
