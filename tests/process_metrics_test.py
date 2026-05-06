import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.process_metrics import compute_process_metrics


def write_jsonl(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")


class ProcessMetricsTest(unittest.TestCase):
    def test_metrics_compute_from_saved_trace_only(self):
        with TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "offline-run"
            trace = run_dir / "trajectories" / "codex" / "smoke_prefix_format" / "attempt-1" / "trajectory.jsonl"
            kb = Path(tmpdir) / "kb.json"
            kb.write_text(
                json.dumps(
                    {
                        "entries": [
                            {
                                "id": "b_001",
                                "instance_id": "smoke_prefix_format",
                                "type": "missing_information",
                                "description": "Missing prefix convention.",
                                "resolution": "Use prefix before name.",
                                "trigger_questions": ["Where does the prefix go?"],
                                "resolution_source": "human",
                                "action_critical": True,
                                "observable_after": None,
                                "commit_boundary": None,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            write_jsonl(
                trace,
                [
                    {
                        "type": "human_input_raw_event",
                        "event_index": 0,
                        "request_id": "q1",
                        "request_type": "clarification",
                        "native_event_type": "codex.item/tool/requestUserInput",
                        "raw_event": {"questions": [{"id": "prefix", "question": "Where does the prefix go?"}]},
                    },
                    {
                        "type": "human_input_normalized_event",
                        "event_index": 1,
                        "event_type": "clarification_request",
                        "request_id": "q1",
                        "request": {"request_id": "q1", "request_type": "clarification", "normalized_question": "Where does the prefix go?"},
                        "question": "Where does the prefix go?",
                    },
                    {
                        "type": "human_input_result",
                        "event_index": 2,
                        "event_type": "clarification_answer",
                        "request_id": "q1",
                        "request_type": "clarification",
                        "result": {
                            "status": "answered",
                            "blocker_id": "b_001",
                            "resolution": "Use prefix before name.",
                            "source": {"kb_hash": "kb", "blocker_id": "b_001"},
                            "oracle": {"prompt_hash": "prompt", "model_id": "bedrock/qwen.qwen3-32b-v1:0"},
                            "cache": {"hit": False, "key": "cache"},
                        },
                    },
                    {
                        "event_index": 2,
                        "type": "human_input_raw_event",
                        "request_id": "a1",
                        "request_type": "approval",
                        "native_event_type": "codex.item/commandExecution/requestApproval",
                        "raw_event": {"command": "npm test"},
                    },
                    {
                        "type": "human_input_normalized_event",
                        "event_index": 3,
                        "event_type": "approval_request",
                        "request_id": "a1",
                        "request": {"request_id": "a1", "request_type": "approval", "normalized_question": "Approve npm test?"},
                    },
                    {
                        "type": "human_input_approval_decision",
                        "event_index": 4,
                        "event_type": "approval_result",
                        "request_id": "a1",
                        "request_type": "approval",
                        "decision": {"allowed": True, "grounding": "fallback", "registry_status": "unknown"},
                    },
                    {"type": "sdk_event", "event_index": 5, "event_type": "file_edit", "files_changed": ["labeler.py"]},
                    {"type": "sdk_event", "event_index": 6, "event_type": "test", "tests_run": [{"command": "npm test", "code": 0}]},
                    {"type": "submission", "event_index": 7, "event_type": "patch_submit", "patch_path": "patch.diff"},
                    {"type": "attempt_end", "event_index": 8, "event_type": "final", "final_status": "unknown"},
                ],
            )
            metrics = compute_process_metrics(
                run_dir,
                kb,
                pass_by_prefix={"offline-run__codex__smoke_prefix_format__attempt-1": True},
            )

        self.assertEqual(metrics["answered_clarification_count"], 1)
        self.assertEqual(metrics["Q_count"], 1)
        self.assertEqual(metrics["Qrel_count"], 1)
        self.assertEqual(metrics["B_count"], 1)
        self.assertEqual(metrics["Baddr_count"], 1)
        self.assertEqual(metrics["paper_ask_f1"], 1.0)
        self.assertEqual(metrics["approval_fallback_count"], 1)
        self.assertEqual(metrics["grounded_pass_count"], 1)
        self.assertEqual(metrics["ungrounded_pass_count"], 0)
        self.assertEqual(metrics["ASK_F1"], 1.0)
        self.assertTrue(metrics["trace_completeness"]["human_facing_events_have_responses"])
        self.assertTrue(metrics["trace_completeness"]["human_facing_events_have_raw_native_payload"])
        self.assertTrue(metrics["trace_completeness"]["human_facing_events_have_normalized_request_type"])
        self.assertTrue(metrics["trace_completeness"]["ask_human_calls_have_audit_cache_metadata"])

    def test_human_request_response_pairing_uses_request_ids(self):
        with TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "offline-run"
            trace = run_dir / "trajectories" / "codex" / "smoke_prefix_format" / "attempt-1" / "trajectory.jsonl"
            write_jsonl(
                trace,
                [
                    {
                        "type": "human_input_raw_event",
                        "event_index": 0,
                        "request_id": "q1",
                        "request_type": "clarification",
                        "native_event_type": "codex.item/tool/requestUserInput",
                        "raw_event": {"question": "Where does the prefix go?"},
                    },
                    {
                        "type": "human_input_normalized_event",
                        "event_index": 1,
                        "event_type": "clarification_request",
                        "request_id": "q1",
                        "request": {"request_id": "q1", "request_type": "clarification", "normalized_question": "Where does the prefix go?"},
                    },
                    {
                        "type": "human_input_result",
                        "event_index": 2,
                        "event_type": "clarification_answer",
                        "request_id": "q2",
                        "request_type": "clarification",
                        "result": {
                            "status": "unknown",
                            "blocker_id": "UNKNOWN",
                            "resolution": "irrelevant question",
                            "source": {"kb_hash": "kb", "blocker_id": "UNKNOWN"},
                            "oracle": {"prompt_hash": "prompt", "model_id": "bedrock/qwen.qwen3-32b-v1:0"},
                            "cache": {"hit": False, "key": "cache"},
                        },
                    },
                    {"type": "attempt_end", "event_index": 3, "event_type": "final", "final_status": "fail"},
                ],
            )
            metrics = compute_process_metrics(run_dir)

        attempt = metrics["attempts"][0]
        self.assertFalse(metrics["trace_completeness"]["human_facing_events_have_responses"])
        self.assertEqual(attempt["unmatched_human_request_ids"]["clarification"], ["q1"])

    def test_saved_metrics_supply_final_outcomes_for_standalone_process_metrics(self):
        with TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "offline-run"
            trace = run_dir / "trajectories" / "codex" / "smoke_prefix_format" / "attempt-1" / "trajectory.jsonl"
            write_jsonl(
                trace,
                [
                    {"type": "submission", "event_index": 0, "event_type": "patch_submit", "patch_path": "patch.diff"},
                    {"type": "attempt_end", "event_index": 1, "event_type": "final", "final_status": "unknown"},
                ],
            )
            (run_dir / "metrics.json").write_text(
                json.dumps(
                    {
                        "harnesses": {
                            "codex": {
                                "instances": {
                                    "smoke_prefix_format": [
                                        {
                                            "prefix": "offline-run__codex__smoke_prefix_format__attempt-1",
                                            "attempt_index": 1,
                                            "resolved": True,
                                        }
                                    ]
                                }
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            metrics = compute_process_metrics(run_dir)

        self.assertEqual(metrics["attempts"][0]["passed"], True)
        self.assertTrue(metrics["trace_completeness"]["final_outcome_exists"])

    def test_paper_ask_precision_counts_unique_blockers_not_duplicate_answered_questions(self):
        with TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "offline-run"
            trace = run_dir / "trajectories" / "codex" / "smoke_prefix_format" / "attempt-1" / "trajectory.jsonl"
            kb = Path(tmpdir) / "kb.json"
            kb.write_text(
                json.dumps(
                    {
                        "entries": [
                            {
                                "id": "b_001",
                                "instance_id": "smoke_prefix_format",
                                "type": "missing_information",
                                "description": "Missing prefix convention.",
                                "resolution": "Use prefix before name.",
                                "action_critical": True,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            write_jsonl(
                trace,
                [
                    {
                        "type": "human_input_normalized_event",
                        "event_index": 0,
                        "request_id": "q1",
                        "request": {"request_id": "q1", "request_type": "clarification", "normalized_question": "Where does the prefix go?"},
                    },
                    {
                        "type": "human_input_result",
                        "event_index": 1,
                        "request_id": "q1",
                        "request_type": "clarification",
                        "result": {
                            "status": "answered",
                            "blocker_id": "b_001",
                            "resolution": "Use prefix before name.",
                            "source": {"kb_hash": "kb", "blocker_id": "b_001"},
                            "oracle": {"prompt_hash": "prompt", "model_id": "judge"},
                            "cache": {"hit": False, "key": "cache"},
                        },
                    },
                    {
                        "type": "human_input_normalized_event",
                        "event_index": 2,
                        "request_id": "q2",
                        "request": {"request_id": "q2", "request_type": "clarification", "normalized_question": "Again, where does the prefix go?"},
                    },
                    {
                        "type": "human_input_result",
                        "event_index": 3,
                        "request_id": "q2",
                        "request_type": "clarification",
                        "result": {
                            "status": "answered",
                            "blocker_id": "b_001",
                            "resolution": "Use prefix before name.",
                            "source": {"kb_hash": "kb", "blocker_id": "b_001"},
                            "oracle": {"prompt_hash": "prompt", "model_id": "judge"},
                            "cache": {"hit": True, "key": "cache"},
                        },
                    },
                    {"type": "submission", "event_index": 4, "event_type": "patch_submit", "patch_path": "patch.diff"},
                ],
            )

            metrics = compute_process_metrics(run_dir, kb)

        self.assertEqual(metrics["question_precision"], 1.0)
        self.assertEqual(metrics["paper_ask_precision"], 0.5)
        self.assertEqual(metrics["paper_ask_recall"], 1.0)
        self.assertAlmostEqual(metrics["paper_ask_f1"], 2 / 3)


if __name__ == "__main__":
    unittest.main()
