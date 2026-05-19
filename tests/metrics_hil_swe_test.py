"""Regression tests for micro ask metrics in metrics_hil_swe.summarize."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "metrics_hil_swe", ROOT / "scripts" / "metrics_hil_swe.py"
)
metrics = importlib.util.module_from_spec(spec)
sys.modules["metrics_hil_swe"] = metrics
spec.loader.exec_module(metrics)


def _row(uid: str, pass_index: int, resolved: int, total: int, questions: int = 1) -> dict:
    return {
        "uid": uid,
        "mode": "ask_human",
        "agent": "codex",
        "model": "gpt-5.5",
        "with_custom_tool": False,
        "with_skill": False,
        "with_ask_guidance": False,
        "pass_index": pass_index,
        "status": "resolved",
        "resolved": True,
        "num_blockers_resolved": resolved,
        "num_blockers_total": total,
        "num_questions": questions,
        "num_total_questions": questions,
        "num_steps": 10,
        "pass_dir": f"/tmp/{uid}/p{pass_index}",
    }


class MetricsMicroTest(unittest.TestCase):
    KEY = "ask_human/codex/gpt-5.5/custom_tool=0/skill=0/ask_guidance=0"

    def test_micro_recall_bounded_for_inflated_event_style_counts(self):
        """Capped micro keeps R in [0,1] even if stored counts exceed registry size."""
        rows = [
            _row("u1", 1, 7, 4, 8),
            _row("u1", 2, 3, 5, 4),
            _row("u1", 3, 2, 3, 2),
        ]
        out = metrics.summarize(rows, expected_passes=3, include_partial=False)
        m = out[self.KEY]
        self.assertGreaterEqual(m["ask_recall"], 0.0)
        self.assertLessEqual(m["ask_recall"], 1.0)
        self.assertGreaterEqual(m["ask_precision"], 0.0)
        self.assertLessEqual(m["ask_precision"], 1.0)
        # micro recall = min(1, (7+3+2) / (4+5+3)) = 1.0
        self.assertAlmostEqual(m["ask_recall"], 1.0, places=3)
        # micro precision = min(1, (7+3+2) / (8+4+2)) = 12/14
        self.assertAlmostEqual(m["ask_precision"], 12 / 14, places=3)

    def test_unique_blockers_per_pass_recall(self):
        rows = [_row("u1", 1, 3, 5, 3)]
        out = metrics.summarize(rows, expected_passes=1, include_partial=True)
        m = out[self.KEY]
        self.assertAlmostEqual(m["ask_recall"], 0.6)
        self.assertAlmostEqual(m["ask_precision"], 1.0)

    def test_event_micro_alias_matches_primary_micro(self):
        rows = [_row("u1", 1, 7, 4, 8)]
        out = metrics.summarize(rows, expected_passes=1, include_partial=True)
        m = out[self.KEY]
        self.assertAlmostEqual(m["ask_precision_event_micro"], m["ask_precision"])
        self.assertAlmostEqual(m["ask_recall_event_micro"], m["ask_recall"])


if __name__ == "__main__":
    unittest.main()
