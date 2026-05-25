"""
Unit tests for run_antigravity.py helper behavior.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src" / "hil_swe") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src" / "hil_swe"))

import run_antigravity as mod


def _any_template_basename(ext: str) -> str:
    template_dir = _ROOT / "src" / "hil_swe" / "templates"
    matches = sorted(template_dir.glob(f"*.{ext}"))
    if not matches:
        raise RuntimeError(f"No .{ext} templates found in {template_dir}")
    return matches[0].stem


class TestLitellmShim(unittest.TestCase):
    def test_litellm_base_url_rewrites_localhost(self):
        self.assertEqual(
            mod._litellm_base_url("http://localhost:4000/v1/"),
            "http://host.docker.internal:4000/v1",
        )

    def test_reasoning_effort_maps_to_thinking_level(self):
        self.assertEqual(str(mod._antigravity_thinking_level("high")), str(mod.ThinkingLevel.HIGH))
        self.assertEqual(str(mod._antigravity_thinking_level("medium")), str(mod.ThinkingLevel.MEDIUM))
        self.assertEqual(str(mod._antigravity_thinking_level("low")), str(mod.ThinkingLevel.LOW))
        self.assertIsNone(mod._antigravity_thinking_level(""))

    def test_custom_mcp_bridge_server_carries_env(self):
        server = mod._custom_mcp_stdio_server("http://127.0.0.1:8123")
        self.assertEqual(server.command, "env")
        self.assertEqual(server.args[0], "SIDECAR_URL=http://127.0.0.1:8123")
        self.assertEqual(server.args[1], "NATIVE_EVENT_TYPE=antigravity.mcp.ask_human")
        self.assertEqual(server.args[2], "node")
        self.assertEqual(server.args[3], mod._BRIDGE_SCRIPT)


class TestGuidanceRendering(unittest.TestCase):
    def test_build_instruction_with_guidance_replaces_tool_placeholder(self):
        old_enabled = mod.ASK_HUMAN_GUIDANCE_ENABLED
        old_version = mod.ASK_HUMAN_GUIDANCE_TEMPLATE_VERSION
        version = _any_template_basename("txt")
        expected = (
            (_ROOT / "src" / "hil_swe" / "templates" / f"{version}.txt")
            .read_text(encoding="utf-8")
            .replace("{{TOOL_NAME}}", mod.SKILL_TOOL_REF)
            .strip()
        )
        mod.ASK_HUMAN_GUIDANCE_TEMPLATE_VERSION = version
        mod.ASK_HUMAN_GUIDANCE_ENABLED = True
        try:
            instr = mod._build_instruction("ask_human")
        finally:
            mod.ASK_HUMAN_GUIDANCE_ENABLED = old_enabled
            mod.ASK_HUMAN_GUIDANCE_TEMPLATE_VERSION = old_version
        self.assertIn(expected, instr)


class TestTrajectoryExtraction(unittest.TestCase):
    def test_native_ask_is_canonicalized(self):
        steps = mod.extract_trajectory_steps(
            [{"type": "antigravity_native_ask", "question": "Q?", "answer": "A!"}],
            [],
        )
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["act"], "ask_human [native] Q?")
        self.assertEqual(steps[0]["obs"], "A!")

    def test_custom_ask_empty_obs_maps_to_cant_answer(self):
        events = [
            {"type": "antigravity_tool_call", "tool_name": "ask_human", "args": {"question": "Need?"}, "call_id": "c1"},
            {"type": "antigravity_tool_result", "tool_name": "ask_human", "call_id": "c1", "result": ""},
        ]
        steps = mod.extract_trajectory_steps(events, [])
        self.assertEqual(len(steps), 1)
        self.assertTrue(steps[0]["act"].startswith("ask_human [custom_tool] "))
        self.assertEqual(steps[0]["obs"], mod.CANT_ANSWER)

    def test_native_and_custom_ask_are_distinguished_when_both_exist(self):
        events = [
            {"type": "antigravity_native_ask", "question": "Native Q?", "answer": "Native A"},
            {"type": "antigravity_tool_call", "tool_name": "human_input.ask_human", "args": {"question": "Custom Q?"}, "call_id": "c2"},
            {"type": "antigravity_tool_result", "tool_name": "human_input.ask_human", "call_id": "c2", "result": "Custom A"},
        ]
        steps = mod.extract_trajectory_steps(events, [])
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0]["act"], "ask_human [native] Native Q?")
        self.assertEqual(steps[0]["obs"], "Native A")
        self.assertEqual(steps[1]["act"], "ask_human [custom_tool] Custom Q?")
        self.assertEqual(steps[1]["obs"], "Custom A")

    def test_tool_obs_prefers_structured_content_over_short_result(self):
        events = [
            {"type": "antigravity_tool_call", "tool_name": "view_file", "args": {"file_path": "/tmp/x"}, "call_id": "c1"},
            {
                "type": "antigravity_tool_result",
                "tool_name": "view_file",
                "call_id": "c1",
                "result": {
                    "result": "View file",
                    "structuredContent": {"file_path": "/tmp/x", "content": "hello"},
                },
            },
        ]
        steps = mod.extract_trajectory_steps(events, [])
        self.assertIn('"content": "hello"', steps[0]["obs"])

    def test_thoughts_are_attached_to_steps_not_only_trailing_empty_rows(self):
        events = [
            {"type": "antigravity_tool_call", "tool_name": "view_file", "args": {"file_path": "/tmp/x"}, "call_id": "c1"},
            {"type": "antigravity_tool_result", "tool_name": "view_file", "call_id": "c1", "result": "ok"},
        ]
        steps = mod.extract_trajectory_steps(events, ["first-thought", "second-thought"])
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0]["act"], "view_file /tmp/x")
        self.assertIn("first-thought", steps[0]["thought"])
        self.assertIn("second-thought", steps[1]["thought"])


class TestStats(unittest.TestCase):
    def test_compute_stats_counts_full_info_and_resolved_blockers(self):
        events = [
            {"type": "human_input_raw_event", "request_id": "r1", "request_type": "clarification"},
            {"type": "human_input_result", "request_id": "r1", "result": {"status": "answered", "blocker_id": "b1"}},
            {"type": "ask_question_full_info_mode", "question": "irrelevant?"},
        ]
        stats = mod.compute_stats(events, [{"act": "", "obs": "", "thought": ""}], 4)
        self.assertEqual(stats["num_questions"], 1)
        self.assertEqual(stats["num_questions_full_info"], 1)
        self.assertEqual(stats["num_blockers_resolved"], 1)
        self.assertEqual(stats["num_blockers_total"], 4)


class TestSidecarEventMerge(unittest.TestCase):
    def test_merge_sidecar_events_deduplicates_human_input_by_type_and_request_id(self):
        all_events = [
            {"type": "human_input_raw_event", "request_id": "r1", "question": "Q1"},
            {"type": "human_input_result", "request_id": "r1", "result": {"status": "unknown"}},
            {"type": "antigravity_native_ask", "question": "Q1", "answer": "irrelevant question"},
        ]
        sidecar_events = [
            {"type": "human_input_raw_event", "request_id": "r1", "question": "Q1"},
            {"type": "human_input_result", "request_id": "r1", "result": {"status": "unknown"}},
            {"type": "human_input_raw_event", "request_id": "r2", "question": "Q2"},
            {"type": "human_input_result", "request_id": "r2", "result": {"status": "answered", "blocker_id": "b2"}},
        ]
        mod._merge_sidecar_events(all_events, sidecar_events)
        by_type = {}
        for ev in all_events:
            by_type.setdefault(ev.get("type"), []).append(ev)
        self.assertEqual(len(by_type.get("human_input_raw_event", [])), 2)
        self.assertEqual(len(by_type.get("human_input_result", [])), 2)
        self.assertEqual(
            sorted(str(ev.get("request_id", "")) for ev in by_type["human_input_raw_event"]),
            ["r1", "r2"],
        )
        self.assertEqual(
            sorted(str(ev.get("request_id", "")) for ev in by_type["human_input_result"]),
            ["r1", "r2"],
        )

    def test_slice_sidecar_events_returns_only_current_attempt_tail(self):
        sidecar_events = [
            {"type": "human_input_raw_event", "request_id": "r_old"},
            {"type": "human_input_result", "request_id": "r_old", "result": {"status": "unknown"}},
            {"type": "human_input_raw_event", "request_id": "r_new"},
            {"type": "human_input_result", "request_id": "r_new", "result": {"status": "unknown"}},
        ]
        sliced = mod._slice_sidecar_events(sidecar_events, 2)
        self.assertEqual(len(sliced), 2)
        self.assertEqual(str(sliced[0].get("request_id", "")), "r_new")
        self.assertEqual(str(sliced[1].get("request_id", "")), "r_new")

    def test_retry_sidecar_tail_slice_prevents_question_overcount(self):
        # Final-attempt trajectory has one ask, while sidecar /events still contains
        # one stale ask from an earlier retry attempt.
        all_events = [
            {"type": "human_input_raw_event", "request_id": "r_new", "request_type": "clarification"},
            {"type": "human_input_result", "request_id": "r_new", "result": {"status": "unknown"}},
            {"type": "antigravity_native_ask", "question": "Q new?", "answer": "irrelevant question"},
        ]
        sidecar_events = [
            {"type": "human_input_raw_event", "request_id": "r_old", "request_type": "clarification"},
            {"type": "human_input_result", "request_id": "r_old", "result": {"status": "unknown"}},
            {"type": "human_input_raw_event", "request_id": "r_new", "request_type": "clarification"},
            {"type": "human_input_result", "request_id": "r_new", "result": {"status": "unknown"}},
        ]
        mod._merge_sidecar_events(all_events, mod._slice_sidecar_events(sidecar_events, 2))
        steps = mod.extract_trajectory_steps(all_events, [])
        ask_acts = sum(1 for s in steps if str(s.get("act", "")).startswith("ask_human"))
        stats = mod.compute_stats(all_events, steps, 0)
        self.assertEqual(ask_acts, 1)
        self.assertEqual(stats["num_questions"], 1)


class TestToolResultSidecarEventExtraction(unittest.TestCase):
    def test_extract_sidecar_events_from_tool_result_structured_content(self):
        result = {
            "structuredContent": {
                "resolution": "irrelevant question",
                "events": [
                    {"type": "human_input_raw_event", "request_id": "r1"},
                    {"type": "human_input_result", "request_id": "r1", "result": {"status": "unknown"}},
                ],
            }
        }
        events = mod._extract_sidecar_events_from_tool_result(result)
        self.assertEqual(len(events), 2)
        self.assertEqual(str(events[0].get("type", "")), "human_input_raw_event")
        self.assertEqual(str(events[1].get("type", "")), "human_input_result")

    def test_extract_sidecar_events_from_tool_result_handles_missing_events(self):
        self.assertEqual(mod._extract_sidecar_events_from_tool_result({}), [])
        self.assertEqual(mod._extract_sidecar_events_from_tool_result("text"), [])


class TestRetryIsolationScenario(unittest.TestCase):
    def test_first_attempt_error_second_attempt_success_keeps_only_second_attempt_sidecar_events(self):
        # Model a retry run where attempt 1 produced sidecar events then failed,
        # attempt 2 succeeded with one native ask. Final sidecar snapshot still
        # contains attempt-1 + attempt-2 events.
        attempt1_sidecar = [
            {"type": "human_input_raw_event", "request_id": "r_old", "request_type": "clarification"},
            {"type": "human_input_result", "request_id": "r_old", "result": {"status": "unknown"}},
        ]
        attempt2_sidecar = [
            {"type": "human_input_raw_event", "request_id": "r_new", "request_type": "clarification"},
            {"type": "human_input_result", "request_id": "r_new", "result": {"status": "answered", "blocker_id": "b_new"}},
        ]
        # Final in-memory event stream from successful attempt 2 before teardown.
        all_events = [
            {"type": "antigravity_native_ask", "question": "Q new?", "answer": "A new"},
        ]
        final_sidecar_snapshot = [*attempt1_sidecar, *attempt2_sidecar]
        # This mirrors retry fallback mode where reset failed and we snapshot
        # existing sidecar length before attempt 2 starts.
        start_index_for_attempt2 = len(attempt1_sidecar)

        mod._merge_sidecar_events(
            all_events,
            mod._slice_sidecar_events(final_sidecar_snapshot, start_index_for_attempt2),
        )
        steps = mod.extract_trajectory_steps(all_events, [])
        stats = mod.compute_stats(all_events, steps, 1)

        ask_acts = sum(1 for s in steps if str(s.get("act", "")).startswith("ask_human"))
        self.assertEqual(ask_acts, 1)
        self.assertEqual(stats["num_questions"], 1)
        self.assertEqual(stats["num_blockers_resolved"], 1)


class TestHistoryErrorDetection(unittest.TestCase):
    class _Step:
        def __init__(self, *, error: str = "", content: str = ""):
            self.error = error
            self.content = content

    def test_history_sdk_error_prefers_step_error_field(self):
        history = [self._Step(error="boom"), self._Step(content="ok")]
        self.assertEqual(mod._history_sdk_error(history), "boom")

    def test_history_sdk_error_falls_back_to_system_step_error_content(self):
        history = [self._Step(content='System step error (HTTP 500): Agent execution terminated due to error.')]
        self.assertIn("System step error", mod._history_sdk_error(history) or "")


class TestTokenLimitDetection(unittest.TestCase):
    def test_is_token_limit_error_matches_known_messages(self):
        self.assertTrue(
            mod._is_token_limit_error(
                'System step error (HTTP 0): The model\'s generation exceeded the maximum output token limit.'
            )
        )
        self.assertTrue(mod._is_token_limit_error("ContextWindowExceeded"))
        self.assertFalse(mod._is_token_limit_error("network connection reset"))

    def test_is_token_limit_structured_matches_error_code_fields(self):
        self.assertTrue(
            mod._is_token_limit_structured(
                {"error": {"codexErrorInfo": {"type": "ContextWindowExceeded"}}}
            )
        )
        self.assertTrue(
            mod._is_token_limit_structured(
                {"error_code": "max_output_tokens"}
            )
        )
        self.assertFalse(mod._is_token_limit_structured({"error_code": "permission_denied"}))


if __name__ == "__main__":
    unittest.main()
