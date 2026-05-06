import json
import tempfile
import unittest
from pathlib import Path

from scripts.validate_trajectories import validate_run


def event(index, event_type="tool_result", type_="sdk_event", **overrides):
    base = {
        "type": type_,
        "timestamp": "2026-05-06T00:00:00.000Z",
        "run_id": "schema-smoke",
        "instance_id": "inst",
        "harness": "codex",
        "attempt_index": 1,
        "event_index": index,
        "event_type": event_type,
        "native_event_type": None,
        "native_payload": None,
        "normalized_request_type": None,
        "content": None,
        "tool_name": None,
        "tool_args": None,
        "observation": None,
        "question": None,
        "answer": None,
        "ask_human_status": "not_applicable",
        "matched_blocker_ids": [],
        "matched_source_ids": [],
        "approval_decision": "not_applicable",
        "approval_grounding": "not_applicable",
        "files_changed": [],
        "commands_run": [],
        "tests_run": [],
        "patch_path": None,
        "final_status": "unknown",
        "audit": {},
    }
    base.update(overrides)
    return base


class TrajectorySchemaTest(unittest.TestCase):
    def test_validate_run_accepts_complete_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "schema-smoke"
            traj = run_dir / "trajectories" / "codex" / "inst" / "attempt-1" / "trajectory.jsonl"
            traj.parent.mkdir(parents=True)
            rows = [
                event(0, type_="attempt_start"),
                event(
                    1,
                    event_type="clarification_request",
                    type_="human_input_normalized_event",
                    normalized_request_type="clarification",
                    tool_args={"request_id": "abc"},
                    question="Which accepted value should be used?",
                ),
                event(
                    2,
                    event_type="clarification_answer",
                    type_="human_input_result",
                    normalized_request_type="clarification",
                    tool_args={"request_id": "abc"},
                    answer="Use active.",
                    ask_human_status="answered",
                    matched_blocker_ids=["b1"],
                ),
                event(3, event_type="patch_submit", type_="submission", patch_path="patch.diff"),
                event(4, event_type="final", type_="attempt_end", final_status="unknown"),
            ]
            traj.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

            report = validate_run(run_dir)
            self.assertTrue(report["valid"], report["errors"])
            self.assertEqual(report["trajectory_files"], 1)

    def test_validate_run_rejects_orphaned_human_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "schema-smoke"
            traj = run_dir / "trajectories" / "codex" / "inst" / "attempt-1" / "trajectory.jsonl"
            traj.parent.mkdir(parents=True)
            rows = [
                event(0, type_="attempt_start"),
                event(
                    1,
                    event_type="clarification_request",
                    type_="human_input_normalized_event",
                    normalized_request_type="clarification",
                    tool_args={"request_id": "abc"},
                    question="Which accepted value should be used?",
                ),
                event(2, event_type="patch_submit", type_="submission", patch_path="patch.diff"),
                event(3, event_type="final", type_="attempt_end", final_status="unknown"),
            ]
            traj.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

            report = validate_run(run_dir)
            self.assertFalse(report["valid"])
            self.assertTrue(any("missing results" in error for error in report["errors"]))


if __name__ == "__main__":
    unittest.main()
