import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.leakage_audit import candidate_files, hidden_terms


class LeakageAuditTest(unittest.TestCase):
    def test_hidden_terms_include_blocker_ids_and_resolutions(self):
        with TemporaryDirectory() as tmpdir:
            kb = Path(tmpdir) / "kb.json"
            kb.write_text(
                json.dumps(
                    {
                        "entries": [
                            {
                                "id": "blocker-secret-id",
                                "blocker_id": "blocker-secret-id",
                                "instance_id": "i",
                                "resolution": "Use the hidden resolution value.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            terms = hidden_terms(kb)
            full_info_terms = hidden_terms(kb, include_resolutions=False)

        self.assertIn("blocker-secret-id", terms)
        self.assertIn("Use the hidden resolution value.", terms)
        self.assertIn("blocker-secret-id", full_info_terms)
        self.assertNotIn("Use the hidden resolution value.", full_info_terms)

    def test_candidate_files_are_agent_visible_artifacts_only(self):
        with TemporaryDirectory() as tmpdir:
            run = Path(tmpdir) / "run"
            attempt = run / "trajectories" / "codex" / "i" / "attempt-1"
            repo_file = attempt / "repo" / "src" / "app.py"
            repo_file.parent.mkdir(parents=True)
            repo_file.write_text("print('visible')\n", encoding="utf-8")
            (attempt / "prompt.md").write_text("visible prompt\n", encoding="utf-8")
            (attempt / "trajectory.jsonl").write_text('{"answer":"hidden after run"}\n', encoding="utf-8")

            files = {path.name for path in candidate_files(run)}

        self.assertIn("prompt.md", files)
        self.assertIn("app.py", files)
        self.assertNotIn("trajectory.jsonl", files)


if __name__ == "__main__":
    unittest.main()
