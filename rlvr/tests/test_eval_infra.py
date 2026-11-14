"""eval.py: infrastructure failures are excluded from metrics (solve None); agent failures count as unsolved."""
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("harbor")
sys.path.insert(0, str(Path(__file__).parents[1]))
spec = importlib.util.spec_from_file_location("rlvr_eval", Path(__file__).parents[1] / "eval.py")
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)


def row(exc_type, exc="", reward=None):
    return {"exception_type": exc_type, "exception": exc, "reward": reward}


def test_infra_errors_are_excluded():
    for r in (row("EnvironmentStartTimeoutError"), row("AgentSetupTimeoutError"), row("AddTestsDirError"),
              row("RewardFileNotFoundError"), row("AuthError", "token validation failed"),
              row("ServiceError", "Could not claim the sandbox name"), row("ResourceExhaustedError"),
              row("RuntimeError", "Docker compose command failed for environment x. Command: docker compose ..."),
              row("RuntimeError", "Failed to start tmux session. Error: None"),
              row("RuntimeError", "t__p1: failed to send non-blocking keys: command=\"tmux send-keys -t terminus-2 ...")):
        assert ev.infra_error(r) and ev.solve_of(r, {}) is None, r


def test_agent_failures_count_as_unsolved():
    for r in (row("AgentTimeoutError"), row("ContextLengthExceededError"), row("RuntimeError", "model output invalid")):
        assert not ev.infra_error(r) and ev.solve_of(r, {}) == 0.0, r


def test_verified_trial_uses_official_solve():
    assert ev.solve_of(row(None, reward=1.0), {"solve": 1.0}) == 1.0
